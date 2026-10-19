"""Stage 2 · the enricher role: backlog -> requests of at most 50 texts -> validated records.

Code owns everything except reading the language: leasing work, packing requests, validating
every returned line, retrying invalid output once, quarantining, budgets and checkpoints.
"""
from __future__ import annotations

import json
import random
import sys
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache

from .budget import BudgetStop, Rates, admit
from .config import INTENT_CODES, PROMPTS, TOPIC_CODES, Settings, label_config
from .db import tx
from .providers import ModelRequest, ModelResponse, Provider
from .textproc import entities, estimate_tokens, render, segments

FIELDS = 10  # n|topic|sub|intent|sev|sent|q|rev|lim|plan
# lim is a letter (y/n), not a digit: with q|rev|lim as three binary digits Haiku dropped one of them in whole
# requests (evals/format_probe/summary.md: 79 of 800 lines failed with digits, 0 of 800 with letters).
LIMITS = {"y": True, "n": False}
PLANS = {"p": "paid", "f": "free", "u": "unstated"}


@dataclass
class Item:
    idx: int
    text_sha: str
    text: str
    origin_id: str
    segs: list[str]
    doubt: str | None = None


def taxonomy_block(tax: dict) -> str:
    code_of = {topic: code for code, topic in TOPIC_CODES.items()}
    lines = []
    for topic, subs in tax["topics"].items():
        options = "; ".join(f"{n} {sub['name']} ({sub['definition']})" for n, sub in enumerate(subs))
        lines.append(f"{code_of[topic]}: {options}")
    return "\n".join(lines)


@lru_cache(maxsize=8)
def _template(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def system_prompt(step: str, tax: dict) -> str:
    template = _template("enrich_v2.md" if step == "primary" else "fallback_v2.md")
    return (template.replace("{{LABELS}}", _template("labels.md").strip())
            .replace("{{TAXONOMY}}", taxonomy_block(tax)).strip())


def parse(output: str, items: list[Item], tax: dict, stop_reason: str | None = None,
          refusal_category: str | None = None, check=None) -> tuple[dict[int, dict], dict[int, str]]:
    """Validate every returned line. Returns (records by index, error by index).

    A line is accepted only if its number belongs to this request, appears once, and every
    field is in range. Anything else is an error for that review alone, never for the batch.
    `check(parts, item, tax)` validates one line's fields; the diagnose stage passes its own.
    """
    check = check or _check
    by_idx = {item.idx: item for item in items}
    good: dict[int, dict] = {}
    bad: dict[int, str] = {}
    for raw in output.splitlines():
        parts = [p.strip() for p in raw.strip().strip("`").split("|")]
        if not parts[0].isdigit():
            continue  # prose or blank line
        n = int(parts[0])
        item = by_idx.get(n)
        if item is None:
            continue  # an id we never sent cannot be attached to any review
        if n in good or n in bad:
            good.pop(n, None)
            bad[n] = "duplicate_line"
            continue
        error = check(parts, item, tax)
        if isinstance(error, str):
            bad[n] = error
        else:
            good[n] = error
    # Lines written before a refusal or the token cap are kept; the rest say why they are missing.
    missing = {"refusal": f"refusal:{refusal_category or 'unspecified'}", "max_tokens": "max_tokens"}.get(
        stop_reason or "", "missing_line")
    for item in items:
        if item.idx not in good and item.idx not in bad:
            bad[item.idx] = missing
    return good, bad


def _check(parts: list[str], item: Item, tax: dict) -> dict | str:
    if len(parts) != FIELDS:
        return "field_count"
    _, topic_code, sub, intent_code, severity, sentiment, quote, review, limit, plan = parts
    topic, intent = TOPIC_CODES.get(topic_code), INTENT_CODES.get(intent_code)
    if topic is None:
        return "unknown_topic"
    if intent is None:
        return "unknown_intent"
    try:
        sub, severity, sentiment, quote = int(sub), int(severity), int(sentiment), int(quote)
    except ValueError:
        return "non_integer_field"
    subtopics = tax["topics"][topic]
    unknown_sub = not 0 <= sub < len(subtopics)
    if unknown_sub:  # costs no retry: the topic is valid, so fall back to its catch-all and flag it
        sub = 0
    if not 1 <= severity <= 5:
        return "severity_range"
    if not -10 <= sentiment <= 10:
        return "sentiment_range"
    if review not in ("0", "1"):
        return "review_flag"
    if limit not in LIMITS:
        return "limit_flag"
    if plan not in PLANS:
        return "unknown_plan"
    # A quote index outside the segments costs no retry: the whole text is always an exact substring.
    evidence = item.segs[quote] if 0 <= quote < len(item.segs) else item.text.strip()
    record = {"topic": topic, "subtopic": subtopics[sub]["id"], "intent": intent, "sentiment": sentiment / 10,
              "severity": severity, "entities": entities(item.text), "evidence_quote": evidence,
              "needs_review": review == "1", "free_tier_limit": LIMITS[limit], "plan": PLANS[plan]}
    # Code-side doubts: label combinations the shared definitions rule out.
    if intent in ("praise", "request", "unclear") and severity > 1:
        record["doubt"] = "severity_without_problem"
    elif intent == "complaint" and severity == 1:
        record["doubt"] = "complaint_without_severity"
    elif unknown_sub:
        record["doubt"] = "unknown_subtopic"
    elif record["needs_review"]:
        record["doubt"] = "model_flag"
    if "doubt" in record:
        record["needs_review"] = True
    return record


class Enricher:
    role = "enrich"  # requests and provider batches are collected per role, never across roles

    def __init__(self, conn, provider: Provider, settings: Settings, rates: Rates, tax: dict, invocation: int,
                 max_requests: int | None = None, sleep=time.sleep):
        self.conn, self.provider, self.s, self.rates, self.tax = conn, provider, settings, rates, tax
        self.invocation = invocation
        self.label_config = label_config(settings, tax)
        self.max_requests, self.requests_made, self.sleep = max_requests, 0, sleep
        self.stopped: str | None = None
        self.batch_errors = 0  # consecutive Batch API failures (network, 5xx); progress stays saved

    def _lanes(self, step: str) -> bool:
        """Whether this step keeps first-time texts and retries in separate lanes."""
        return step == "primary"

    def _release(self) -> int:
        return release_stale(self.conn)

    # ── leasing ────────────────────────────────────────────────────────────────
    def _lease(self, step: str, tier: str, retry_lane: bool | None = None) -> tuple[ModelRequest, list[Item]] | None:
        primary = step == "primary"
        where = "status='pending'" if primary else "status='fallback_pending'"
        if retry_lane is not None:  # anything that failed before, counted or not, goes to the retry lane
            where += " AND (attempts>0 OR reason IS NOT NULL)" if retry_lane else " AND attempts=0 AND reason IS NULL"
        limit = (self.s.retry_batch_size if retry_lane else self.s.batch_size) if primary else 1
        system = system_prompt(step, self.tax)
        with tx(self.conn):
            rows = self.conn.execute(
                f"SELECT text_sha, text, origin_review_id, doubt, reason FROM texts WHERE {where} ORDER BY "
                + ("coalesce(reason LIKE 'refusal%', 0) DESC, " if retry_lane else "") + "n_chars, text_sha LIMIT ?",
                (limit,)).fetchall()
            if not rows:
                return None
            if (rows[0]["reason"] or "").startswith("refusal"):
                rows = rows[:1]
            if self.max_requests is not None and self.requests_made >= self.max_requests:
                self.stopped = "max_requests"
                return None
            items, lines, chars = [], [], 0
            for row in rows:
                segs = segments(row["text"], self.s.quote_full_max)
                line = render(len(items), segs)
                if items and chars + len(line) > self.s.max_request_chars:
                    break
                items.append(Item(len(items), row["text_sha"], row["text"], row["origin_review_id"], segs, row["doubt"]))
                lines.append(line)
                chars += len(line) + 1
            user = "\n".join(lines)
            if not primary:
                user += f"\nDoubt: {items[0].doubt or 'model_flag'}"
            est_in = estimate_tokens(len(system) + len(user))
            max_out = (min(self.s.max_output_tokens, self.s.output_tokens_per_item * len(items) + 40)
                       if primary else self.s.fallback_max_output_tokens)
            reservation = self.rates.cost(self.provider.name, self.provider.model, tier, est_in, 0, max_out)
            try:  # nothing has been written yet, so stopping here leaves the backlog untouched
                admit(self.conn, self.s, step, reservation)
            except BudgetStop as stop:
                self.stopped = f"budget: {stop}"
                return None
            key = f"{step[0]}{self.invocation}-{uuid.uuid4().hex[:14]}"
            self.conn.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                              (key, "enrich", step, "built", None, self.invocation, len(items), est_in, max_out,
                               reservation, time.time()))
            self.conn.executemany("INSERT INTO request_items VALUES(?,?,?)",
                                  [(key, item.idx, item.text_sha) for item in items])
            self.conn.executemany("UPDATE texts SET status='leased', request_key=?, updated_at=? WHERE text_sha=?",
                                  [(key, time.time(), item.text_sha) for item in items])
        self.requests_made += 1
        effort, thinking = (self.s.effort, self.s.thinking) if primary else (self.s.fallback_effort, self.s.fallback_thinking)
        return ModelRequest(key, "enrich", step, system, user, effort, max_out, thinking), items

    def _items_of(self, key: str) -> list[Item]:
        rows = self.conn.execute(
            "SELECT i.idx, t.text_sha, t.text, t.origin_review_id, t.doubt FROM request_items i "
            "JOIN texts t USING(text_sha) WHERE i.request_key=? ORDER BY i.idx", (key,)).fetchall()
        return [Item(r["idx"], r["text_sha"], r["text"], r["origin_review_id"],
                     segments(r["text"], self.s.quote_full_max), r["doubt"]) for r in rows]

    # ── saving ─────────────────────────────────────────────────────────────────
    def _save(self, key: str, step: str, tier: str, items: list[Item], resp: ModelResponse) -> None:
        """One atomic write per request: the call log, every text's new status, the request."""
        good, bad = parse(resp.text, items, self.tax, resp.stop_reason, resp.refusal_category) if resp.ok else ({}, {})
        succeeded = resp.ok and bool(good)
        error = resp.error if not resp.ok else None if good else f"no_valid_lines:{resp.stop_reason}"
        cost = self.rates.cost(self.provider.name, self.provider.model, tier, resp.input_tokens,
                               resp.cached_input_tokens, resp.output_tokens, resp.cache_write_tokens)
        effort = self.s.effort if step == "primary" else self.s.fallback_effort
        now = time.time()
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (resp.request_id, key, "enrich", step, self.provider.name, self.provider.model, effort, tier,
                 self.label_config, self.invocation, "succeeded" if succeeded else "failed", error,
                 resp.input_tokens, resp.cached_input_tokens, resp.cache_write_tokens, resp.output_tokens,
                 resp.reasoning_tokens, int(resp.usage_available), cost, resp.started_at, resp.duration_ms,
                 resp.stop_reason, len(good), len(bad), json.dumps([item.origin_id for item in items]),
                 resp.text))  # raw output kept so failures can be inspected and re-parsed offline
            mine = "WHERE text_sha=? AND request_key=? AND status='leased'"  # never overwrite another worker
            for item in items:
                if not resp.ok:  # nothing came back: not an invalid-output attempt
                    self.conn.execute(
                        f"UPDATE texts SET status=CASE WHEN result IS NULL THEN 'pending' ELSE 'fallback_pending' END,"
                        f" request_key=NULL, updated_at=? {mine}", (now, item.text_sha, key))
                elif item.idx in good:
                    record = good[item.idx]
                    doubt = record.pop("doubt", None)
                    status = "fallback_pending" if step == "primary" and doubt else "completed"
                    self.conn.execute(
                        f"UPDATE texts SET status=?, result=?, doubt=?, step=?, reason=NULL, updated_at=? {mine}",
                        (status, json.dumps(record, ensure_ascii=False), doubt, step, now, item.text_sha, key))
                elif step == "fallback":  # keep the valid primary labels, still flagged for review
                    self.conn.execute(
                        f"UPDATE texts SET status='completed', doubt=doubt || '; fallback_invalid:' || ?, updated_at=? {mine}",
                        (bad[item.idx], now, item.text_sha, key))
                elif len(items) > 1 and bad[item.idx].startswith(("refusal", "max_tokens")):
                    # Cut short by a refusal or the token cap with other reviews in the request: the cause may be
                    # a neighbour, so this is not the text's own invalid output. Retry it alone, no attempt counted.
                    self.conn.execute(
                        f"UPDATE texts SET status='pending', reason=?, updated_at=? {mine}",
                        (bad[item.idx] if bad[item.idx].startswith("refusal") else "refusal:isolate-after-max_tokens",
                         now, item.text_sha, key))
                else:  # invalid output: one retry, then quarantine
                    self.conn.execute(
                        f"UPDATE texts SET attempts=attempts+1, updated_at=?,"
                        f" status=CASE WHEN attempts+1>=2 THEN 'quarantined' ELSE 'pending' END,"
                        f" reason=? {mine}",
                        (now, bad[item.idx] if bad[item.idx].startswith("refusal")
                         else f"invalid_output:{bad[item.idx]}", item.text_sha, key))
            self.conn.execute("UPDATE requests SET status=? WHERE request_key=?",
                              ("done" if resp.ok else "failed", key))

    # ── synchronous lane ───────────────────────────────────────────────────────
    def run_sync(self, step: str = "primary", retry_lane: bool | None = None) -> None:
        failures = 0
        tier = self.provider.sync_tier
        while True:
            if self._lanes(step) and retry_lane is None:  # retries first, so refusal-hit texts go alone
                leased = self._lease(step, tier, True) or self._lease(step, tier, False)
            else:
                leased = self._lease(step, tier, retry_lane)
            if leased is None:
                return
            request, items = leased
            resp = self.provider.complete(request)
            self._save(request.key, step, tier, items, resp)
            if resp.ok:
                failures = 0
                continue
            failures += 1
            if resp.fatal:
                self.stopped = f"fatal: {resp.error}"
                return
            if failures > self.s.transport_retries:
                self.stopped = f"transport: {resp.error}"
                return
            # Bounded exponential backoff with jitter for rate limits and temporary errors.
            backoff = min(60.0, self.s.backoff_seconds * 2 ** (failures - 1)) * (0.5 + random.random())
            self.sleep(max(backoff, resp.retry_after or 0.0))

    # ── provider Batch API lane ────────────────────────────────────────────────
    def _submit_wave(self, step: str) -> int:
        tier = self.provider.batch_tier
        enqueued = self.conn.execute("SELECT COALESCE(SUM(est_input_tokens), 0) FROM provider_batches "
                                     "WHERE status='in_progress'").fetchone()[0]
        requests, wave_tokens = [], 0
        while len(requests) < self.s.batch_file_requests and enqueued + wave_tokens < self.s.max_enqueued_tokens:
            leased = self._lease(step, tier, retry_lane=False if self._lanes(step) else None)
            if leased is None:
                break
            requests.append(leased[0])
            wave_tokens += estimate_tokens(len(leased[0].system) + len(leased[0].user))
        if not requests:
            return 0
        started = time.time()
        try:
            provider_batch = self.provider.submit_batch(requests)
        except Exception as error:  # the provider may or may not have created it: release, record, retry later
            self.batch_errors += 1
            self._release()
            print(f"batch submit failed ({type(error).__name__}); {len(requests)} requests released", file=sys.stderr)
            finder = getattr(self.provider, "unrecorded_batches", None)
            if finder:
                known = {r[0] for r in self.conn.execute("SELECT provider_batch FROM provider_batches")}
                suspects = finder(started, known)
                if suspects:  # possibly created despite the error: left for a person to reconcile, never cancelled
                    print(f"batches created since the failed submit and not recorded: {suspects}", file=sys.stderr)
            return -1
        self.batch_errors = 0
        with tx(self.conn):
            self.conn.execute("INSERT INTO provider_batches VALUES(?,?,?,?,?,?)",
                              (provider_batch, "in_progress", len(requests), wave_tokens, time.time(), None))
            self.conn.executemany("UPDATE requests SET status='submitted', provider_batch=? WHERE request_key=?",
                                  [(provider_batch, r.key) for r in requests])
        return len(requests)

    def _collect(self, step: str) -> int:
        """Ingest every provider batch that finished. Returns how many are still running."""
        running = 0
        batches = [r["provider_batch"] for r in self.conn.execute(
            "SELECT provider_batch FROM provider_batches b WHERE status='in_progress' AND EXISTS (SELECT 1 FROM "
            "requests r WHERE r.provider_batch=b.provider_batch AND r.role=?) ORDER BY submitted_at", (self.role,))]
        for provider_batch in batches:
            try:
                state = self.provider.poll_batch(provider_batch)
                responses = self.provider.fetch_batch(provider_batch) if state != "in_progress" else None
            except Exception as error:  # a network error while polling loses nothing: poll again next cycle
                self.batch_errors += 1
                print(f"polling {provider_batch} failed ({type(error).__name__}); will retry", file=sys.stderr)
                running += 1
                continue
            self.batch_errors = 0
            if state == "in_progress":
                running += 1
                continue
            keys = [r["request_key"] for r in self.conn.execute(
                "SELECT request_key FROM requests WHERE provider_batch=? AND status='submitted'", (provider_batch,))]
            for key in keys:
                resp = responses.get(key) or ModelResponse(
                    request_id=f"{key}:unfinished", ok=False, error=f"batch_{state}", usage_available=False,
                    started_at=time.time())
                self._save(key, step, self.provider.batch_tier, self._items_of(key), resp)
            self.conn.execute("UPDATE provider_batches SET status=?, finished_at=? WHERE provider_batch=?",
                              (state, time.time(), provider_batch))
        return running

    def run_batch(self, step: str = "primary") -> None:
        while True:
            running = self._collect(step)
            if self._lanes(step):
                self.run_sync(step, retry_lane=True)  # invalid-output retries go synchronously
            submitted = 0 if self.stopped else self._submit_wave(step)
            if self.batch_errors > 5 * self.s.transport_retries:
                self.stopped = f"transport: Batch API failed {self.batch_errors} times in a row"
                return
            if not running and not submitted:
                return
            self.sleep(self.s.poll_seconds)

    def run(self, step: str = "primary") -> str | None:
        if self.s.mode == "batch":
            self.run_batch(step)
        else:
            self.run_sync(step)
        return self.stopped


def release_stale(conn) -> int:
    """Return work leased by a process that died before its request was sent.

    Requests already submitted to a provider batch are left alone: they are polled, not re-sent.
    """
    with tx(conn):
        keys = [r["request_key"] for r in conn.execute(
            "SELECT request_key FROM requests WHERE status='built' AND role='enrich'")]
        for key in keys:
            conn.execute("UPDATE texts SET status=CASE WHEN result IS NULL THEN 'pending' ELSE 'fallback_pending' END,"
                         " request_key=NULL WHERE request_key=? AND status='leased'", (key,))
            conn.execute("UPDATE requests SET status='abandoned', reserved_usd=0 WHERE request_key=?", (key,))
    return len(keys)


def resolve_fallback_cap(conn, settings: Settings) -> dict:
    """Keep at most the declared fraction of unique texts for fallback; finish the rest as they are.

    Priority: complaints and cancellations first, then severity, then how many reviews share the text.
    """
    if conn.execute("SELECT COUNT(*) FROM texts WHERE status IN ('pending','leased')").fetchone()[0]:
        raise RuntimeError("Primary enrichment is not finished; run it before the fallback step")
    total = conn.execute("SELECT COUNT(*) FROM texts").fetchone()[0]
    used = conn.execute("SELECT COUNT(*) FROM texts WHERE step='fallback'").fetchone()[0]
    room = max(0, int(total * settings.fallback_fraction) - used)
    with tx(conn):
        ranked = [r["text_sha"] for r in conn.execute(
            "SELECT text_sha FROM texts WHERE status='fallback_pending' ORDER BY "
            "json_extract(result,'$.intent') IN ('complaint','cancellation') DESC, "
            "json_extract(result,'$.severity') DESC, n_reviews DESC, text_sha")]
        conn.executemany("UPDATE texts SET status='completed', doubt=doubt || '; fallback_cap' WHERE text_sha=?",
                         [(sha,) for sha in ranked[room:]])
    return {"doubted": len(ranked) + used, "cap": int(total * settings.fallback_fraction),
            "to_fallback": min(room, len(ranked)), "released_by_cap": max(0, len(ranked) - room)}


def release_unfunded_fallback(conn) -> int:
    """After a budget stop, finish the remaining doubted texts with their primary labels."""
    with tx(conn):
        cursor = conn.execute("UPDATE texts SET status='completed', doubt=doubt || '; fallback_budget' "
                              "WHERE status='fallback_pending'")
    return cursor.rowcount
