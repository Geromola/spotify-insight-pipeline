"""Stage 6 · the memo writer and the memo critic.

The writer sees only saved aggregates and a bounded evidence pack, never the raw corpus. Code
checks every claim value, issue id, review id and number; the critic checks the argument and may
ask for one revision. Only claims that pass the code check reach claims.csv.
"""
from __future__ import annotations

import csv
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .group import examples
from .roles import Writer, cached, digest, remember
from .symptom_tables import VERSION_FLOOR
CONTEXT_FLOOR_PCT = 10.0  # a stated device or trigger is cited only when it covers this share of a symptom

METRICS = ("complaint_count", "severity_sum", "mean_severity", "priority_score")
AREAS = {"access": ["access"], "usability": ["usability"], "playback": ["playback"],
         "billing/support": ["billing", "support"], "outside the question": ["downloads", "catalog", "other"]}

MEMO_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["priority_area", "headline", "memo_markdown", "fixes", "claims", "quotes"],
    "properties": {
        "priority_area": {"type": "string", "enum": ["access", "usability", "playback", "billing/support"]},
        "headline": {"type": "string"},
        "memo_markdown": {"type": "string"},
        # The writer picks the symptoms and words the next step; code renders each bullet from facts (render_memo).
        "fixes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["symptom_id", "next_step"],
            "properties": {"symptom_id": {"type": "string"}, "next_step": {"type": "string"}}}},
        "claims": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["claim_id", "issue_id", "metric", "value"],
            "properties": {"claim_id": {"type": "string"}, "issue_id": {"type": "string"},
                           "metric": {"type": "string", "enum": list(METRICS)}, "value": {"type": "string"}}}},
        "quotes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["issue_id", "quote"],
            "properties": {"issue_id": {"type": "string"}, "quote": {"type": "string"}}}}}}

CRITIC_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "problems"],
    "properties": {"verdict": {"type": "string", "enum": ["pass", "revise"]},
                   "problems": {"type": "array", "items": {"type": "string"}}}}

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CLAIM = re.compile(r"\[(C\d+)\]")
_ISSUE = re.compile(r"\b(access|usability|playback|downloads|catalog|billing|support|other)\.[a-z0-9-]+\b")
_DATE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b")
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_QUOTED = re.compile(r"[\"“]([^\"”]{15,})[\"”]")
_SPACES = re.compile(r"\s+")
_VERSION = re.compile(r"(?<![\w.])\d+(?:\.\d+){2,}\b")  # an app version such as 8.7.90.544
_HEADING = re.compile(r"^#{1,3} ", re.M)

MEMO_PROMPT, CRITIC_PROMPT = "memo_v4.md", "critic_v4.md"
CHECKS_VERSION = "c8"  # bump when validate_memo changes, so a cached memo is written and checked again
DIAGNOSIS_SYMPTOMS = 6       # symptoms per area in the pack, "other" excluded
DIAGNOSIS_QUOTES = 2         # most-liked quotes per symptom
DIAGNOSIS_QUOTE_CHARS = 200  # a diagnosis quote is cut at a word boundary to at most this many characters


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _cut(quote: str, limit: int = DIAGNOSIS_QUOTE_CHARS) -> str:
    """At most `limit` characters, cut at a word boundary; a prefix, so still an exact substring of the quote."""
    if len(quote) <= limit:
        return quote
    head = quote[:limit + 1]
    space = head.rfind(" ")
    return (head[:space] if space > 0 else quote[:limit]).rstrip()


def _num(value, kind=int):
    try:
        return kind(value)
    except (TypeError, ValueError):
        return None


def diagnosis_facts(run_dir: Path) -> dict | None:
    """The diagnosis stage's saved tables, bounded for the writer: the concrete symptoms behind each area."""
    path = run_dir / "diagnosis.json"
    if not path.exists():
        return None
    diagnosis = json.loads(path.read_text(encoding="utf-8"))
    load = lambda name: _read_csv(run_dir / name) if (run_dir / name).exists() else []
    table = {(r["area"], r["symptom_id"]): r for r in load("diagnosis_symptoms.csv")}
    customers = {r["area"]: {k: _num(v) for k, v in r.items() if k != "area"} for r in load("diagnosis_customers.csv")}
    check_path = run_dir / "diagnosis_verify.json"
    check = json.loads(check_path.read_text(encoding="utf-8")) if check_path.exists() else None
    areas = {}
    for area, entries in diagnosis["areas"].items():
        total = sum(e["complaints"] for e in entries)
        other = sum(e["complaints"] for e in entries if e["symptom_id"] == "other")
        gated = lambda e: bool(e.get("plan_gated", table.get((area, e["symptom_id"]), {}).get("plan_gated") == "1"))
        ranked = sorted((e for e in entries if e["symptom_id"] != "other" and e["complaints"]),
                        key=lambda e: (-e["severity_sum"], e["symptom_id"]))
        # Up to six fixable symptoms, plus every plan-gated one: the memo may cite those only as free-tier friction.
        fixable = [e for e in ranked if not gated(e)][:DIAGNOSIS_SYMPTOMS]
        symptoms = []
        for e in (e for e in ranked if gated(e) or e in fixable):
            row = table.get((area, e["symptom_id"]), {})
            item = {"symptom_id": e["symptom_id"], "name": e["name"], "definition": e["definition"],
                    "complaints": e["complaints"], "severity_sum": e["severity_sum"],
                    "mean_severity": row.get("mean_severity") or None,
                    "first6_complaints": _num(row.get("first6_complaints")),
                    "last6_complaints": e["last6_complaints"], "last6_severity_sum": e["last6_severity_sum"],
                    "paying_complaints": e["paying_complaints"], "likes_sum": _num(row.get("likes_sum"))}
            for field in ("context", "trigger"):  # "none" or a sliver (under 10%) says nothing useful: leave it out
                value = e.get(f"top_{field}") or ""
                share = _num(row.get(f"top_{field}_share_pct"), float)
                if value and value != "none" and share is not None and share >= CONTEXT_FLOOR_PCT:
                    item[f"top_{field}"] = value
                    item[f"top_{field}_share_pct"] = share
            reviews = _num(row.get("top_version_reviews"))
            share, overall = e.get("top_version_share_pct"), e.get("overall_share_pct")
            if e.get("top_version") and reviews is not None and reviews >= VERSION_FLOOR \
                    and share is not None and overall is not None and share >= 2 * overall:
                item["version"] = {"app_version": e["top_version"], "pct_of_this_versions_reviews": share,
                                   "pct_of_all_reviews": overall}  # reviews reporting this symptom, on that version vs all
            item["quotes"] = [{"review_id": q["review_id"], "quote": _cut(q["quote"])}
                              for q in sorted(e.get("quotes", []), key=lambda q: (-q.get("likes", 0), q["review_id"]))
                              if q.get("quote")][:DIAGNOSIS_QUOTES]
            if gated(e):
                item["plan_gated"] = True
            symptoms.append({k: v for k, v in item.items() if v is not None})
        areas[area] = {"diagnosed_complaints": total,
                       "other_share_pct": f"{100 * other / total:.1f}" if total else "0.0",
                       "customers": customers.get(area), "symptoms": symptoms}
        if (diagnosis.get("concrete") or {}).get(area) is not None:
            areas[area]["concrete"] = diagnosis["concrete"][area]
    return {"catalog_version": diagnosis["catalog_version"], "coverage": diagnosis["coverage"],
            "verification": {"n": check["n"], "symptom_agreement": check["agreement"]["symptom"],
                             "context_agreement": check["agreement"]["context"]} if check else None,
            "areas": areas}


# A lens quote must come from a review inside that lens, so a quote is never attributed to the wrong group.
LENS_FILTERS = {"without free-tier limits": "c.free_tier_limit=0", "paying reviewers only": "c.plan='paid'"}


def facts(conn, run_dir: Path, settings) -> dict:
    """The only material the writer sees. Every number here was computed by code from saved files."""
    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    ingestion = json.loads((run_dir / "ingestion.json").read_text(encoding="utf-8"))
    ranking = _read_csv(run_dir / "ranking.csv")
    names = json.loads((run_dir / "issue_names.json").read_text(encoding="utf-8")).get("issues", {}) \
        if (run_dir / "issue_names.json").exists() else {}
    trends = _read_csv(run_dir / "trends_month.csv")
    full_months = sorted({t["month"] for t in trends if t["partial_month"] == "0"})
    total = summary["complaints"]

    def share(n: int, d: int) -> str:
        return f"{100 * n / d:.1f}" if d else "0.0"

    top = []
    for row in ranking[:settings.memo_top_issues]:
        issue = row["issue_id"]
        topic, subtopic = issue.split(".", 1)
        trend = {t["month"]: t for t in trends if t["issue_id"] == issue and t["partial_month"] == "0"}
        first, last = (full_months[0], full_months[-1]) if full_months else (None, None)
        top.append({
            "rank": int(row["rank"]), "issue_id": issue, "title": names.get(issue, {}).get("title", issue),
            "description": names.get(issue, {}).get("description", ""),
            "complaint_count": int(row["complaint_count"]), "severity_sum": int(row["severity_sum"]),
            "mean_severity": row["mean_severity"], "priority_score": int(row["priority_score"]),
            "share_of_all_complaints_pct": share(int(row["complaint_count"]), total),
            "cancellation_intent_reviews": conn.execute(
                "SELECT COUNT(*) FROM records WHERE status='completed' AND topic=? AND subtopic=? "
                "AND intent='cancellation'", (topic, subtopic)).fetchone()[0],
            "trend": {"first_full_month": first, "last_full_month": last,
                      "share_of_completed_reviews_first_pct": share(int(trend[first]["complaints"]),
                                                                    int(trend[first]["completed_in_month"]))
                      if first in trend else None,
                      "share_of_completed_reviews_last_pct": share(int(trend[last]["complaints"]),
                                                                   int(trend[last]["completed_in_month"]))
                      if last in trend else None},
            "quotes": examples(conn, "c.intent IN ('complaint','cancellation') AND c.topic=? AND c.subtopic=?",
                               (topic, subtopic), settings.memo_quotes_per_issue),
        })
    areas = {}
    for area, topics in AREAS.items():
        marks = ",".join("?" * len(topics))
        count, severity = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(severity),0) FROM records WHERE status='completed' "
            f"AND intent IN ('complaint','cancellation') AND topic IN ({marks})", topics).fetchone()
        areas[area] = {"topics": topics, "complaints": count, "severity_sum": severity,
                       "share_of_all_complaints_pct": share(count, total)}
    segments = None
    if conn.execute("SELECT COUNT(*) FROM records WHERE plan IS NOT NULL").fetchone()[0]:
        complaint = "status='completed' AND intent IN ('complaint','cancellation')"
        by_area = {}
        for area, topics in AREAS.items():
            marks = ",".join("?" * len(topics))
            row = conn.execute(f"""
                SELECT COUNT(*) n, COALESCE(SUM(severity),0) s,
                       SUM(free_tier_limit=1) lim_n, COALESCE(SUM(CASE WHEN free_tier_limit=1 THEN severity END),0) lim_s,
                       SUM(plan='paid') paid_n, COALESCE(SUM(CASE WHEN plan='paid' THEN severity END),0) paid_s
                FROM records WHERE {complaint} AND topic IN ({marks})""", topics).fetchone()
            n = row["n"] or 0
            by_area[area] = {"complaints": n, "severity_sum": row["s"],
                             "free_tier_limit_complaints": row["lim_n"] or 0, "free_tier_limit_severity_sum": row["lim_s"],
                             "free_tier_limit_share_pct": share(row["lim_n"] or 0, n),
                             "other_complaints": n - (row["lim_n"] or 0), "other_severity_sum": row["s"] - row["lim_s"],
                             "paying_reviewer_complaints": row["paid_n"] or 0,
                             "paying_reviewer_severity_sum": row["paid_s"]}
        plans = {r[0]: r[1] for r in conn.execute(
            f"SELECT plan, COUNT(*) FROM records WHERE {complaint} GROUP BY plan")}

        def window(months: list[str]) -> dict:
            """The same area split inside a run of full months: the question asks about the end of the window."""
            if not months:
                return {}
            out = {"months": f"{months[0]} to {months[-1]}",
                   "completed_reviews": conn.execute(
                       f"SELECT COUNT(*) FROM records WHERE status='completed' AND month IN ({','.join('?' * len(months))})",
                       months).fetchone()[0], "areas": {}}
            for area, topics in AREAS.items():
                row = conn.execute(f"""
                    SELECT COUNT(*) n, COALESCE(SUM(severity),0) s,
                           SUM(free_tier_limit=0) o_n, COALESCE(SUM(CASE WHEN free_tier_limit=0 THEN severity END),0) o_s,
                           SUM(plan='paid') p_n, COALESCE(SUM(CASE WHEN plan='paid' THEN severity END),0) p_s
                    FROM records WHERE {complaint} AND topic IN ({','.join('?' * len(topics))})
                    AND month IN ({','.join('?' * len(months))})""", (*topics, *months)).fetchone()
                out["areas"][area] = {"complaints": row["n"] or 0, "severity_sum": row["s"],
                                      "without_free_tier_limits_complaints": row["o_n"] or 0,
                                      "without_free_tier_limits_severity_sum": row["o_s"],
                                      "paying_reviewer_complaints": row["p_n"] or 0,
                                      "paying_reviewer_severity_sum": row["p_s"]}
            return out
        lenses = {}
        seg_rows = _read_csv(run_dir / "ranking_segments.csv") if (run_dir / "ranking_segments.csv").exists() else []
        for lens in ("without free-tier limits", "paying reviewers only"):
            lenses[lens] = [{"rank": int(r["rank"]), "issue_id": r["issue_id"],
                             "title": names.get(r["issue_id"], {}).get("title", r["issue_id"]),
                             "complaint_count": int(r["complaint_count"]), "severity_sum": int(r["severity_sum"]),
                             "mean_severity": r["mean_severity"]}
                            for r in seg_rows if r["lens"] == lens][:settings.memo_top_issues]
        for issue in top:  # the free-tier share and paying reviewers inside each top issue
            t, s = issue["issue_id"].split(".", 1)
            row = conn.execute(f"SELECT COUNT(*) n, SUM(free_tier_limit=1) lim, SUM(plan='paid') paid FROM records "
                               f"WHERE {complaint} AND topic=? AND subtopic=?", (t, s)).fetchone()
            issue["free_tier_limit_share_pct"] = share(row["lim"] or 0, row["n"] or 0)
            issue["paying_reviewer_complaints"] = row["paid"] or 0
        segments = {"definition": "free_tier_limit = the complaint is about something the free version restricts by "
                                  "design (ads between songs, shuffle-only, skip limits, premium-locked features); "
                                  "plan = what the reviewer says about their own plan (paid, free, unstated)",
                    "complaints_by_stated_plan": plans, "by_area": by_area, "lens_rankings": lenses,
                    "windows": {"first_six_full_months": window(full_months[:6]),
                                "last_six_full_months": window(full_months[-6:])}}
        for lens, lens_issues in lenses.items():  # quotes only from that lens's own reviewers
            for issue in lens_issues:
                t, s = issue["issue_id"].split(".", 1)
                issue["quotes"] = examples(conn, "c.intent IN ('complaint','cancellation') AND c.topic=? "
                                                 f"AND c.subtopic=? AND {LENS_FILTERS[lens]}", (t, s), 2)
    verification = json.loads((run_dir / "verify_report.json").read_text(encoding="utf-8")) \
        if (run_dir / "verify_report.json").exists() else None
    pack = {
        "question": "At the end of this historical review window, where should the next quarter of product "
                    "effort go: access, usability, playback, or billing/support?",
        "scope": {"source_rows": summary["source_rows"], "completed_classifications": summary["completed"],
                  "quarantined_or_unclassified": summary["quarantined"], "unique_texts": summary["unique_texts"],
                  "rows_reusing_an_exact_text_result": summary["cache_reuse"],
                  "complaint_or_cancellation_reviews": total, "needs_review_texts": summary["needs_review_texts"],
                  "first_review": ingestion["first_review"], "last_review": ingestion["last_review"],
                  "quarantine_reasons": {r[0]: r[1] for r in conn.execute(
                      "SELECT reason, COUNT(*) FROM records WHERE status='quarantined' GROUP BY reason ORDER BY 1")}},
        "ranking_rule": "priority_score = complaint_count x mean_severity = severity_sum; complaint and "
                        "cancellation reviews only; ties by issue id",
        "ranking": [{k: row[k] for k in ("rank", "issue_id", *METRICS)} for row in ranking],
        "top_issues": top,
        "product_areas": areas,
        "segments": segments,
        "verification": {k: verification[k] for k in ("n", "agreement", "severity_mae", "segment_agreement")
                         if k in verification} if verification else None,
    }
    diagnosis = diagnosis_facts(run_dir)
    if diagnosis is not None:
        pack["diagnosis"] = diagnosis
    return pack


def _numbers(value, out: set) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        out.add(Decimal(str(value)))
    elif isinstance(value, str):
        try:
            out.add(Decimal(value.replace(",", "").rstrip("%")))
        except InvalidOperation:
            pass
    elif isinstance(value, dict):
        for v in value.values():
            _numbers(v, out)
    elif isinstance(value, list):
        for v in value:
            _numbers(v, out)


def _pack_quotes(pack: dict) -> list[dict]:
    quotes = [q for issue in pack["top_issues"] for q in issue["quotes"]]
    for issues in ((pack.get("segments") or {}).get("lens_rankings") or {}).values():
        quotes += [q for issue in issues for q in issue.get("quotes", [])]
    for area in ((pack.get("diagnosis") or {}).get("areas") or {}).values():  # each from that symptom's reviews
        quotes += [q for symptom in area["symptoms"] for q in symptom.get("quotes", [])]
    return quotes


def quote_sources(memo: dict, pack: dict) -> list[dict]:
    """Map each quote the memo uses back to the review it came from (kept out of the prose)."""
    normal = lambda q: _SPACES.sub(" ", q).strip().lower()
    out = []
    for item in memo.get("quotes", []):
        match = next((q for q in _pack_quotes(pack) if normal(item["quote"]) in normal(q["quote"])), None)
        out.append({**item, "review_id": match["review_id"] if match else None})
    return out


MEMO_WORDS = 300  # the writer's own prose; code's fix bullets (about 60 words each, at most four) come on top
MEMO_HEADINGS = ("Recommendation", "What to fix first", "Why", "Free versus premium", "Alternatives", "Limitations")


# Device and trigger words a fix bullet might use, keyed by the diagnosis code words (CONTEXT_CODES, TRIGGER_CODES).
_SETTINGS = {"bluetooth": r"\bbluetooth|headphones?|earbuds?|speakers?\b", "car": r"\bcars?\b|android auto",
             "tv-cast": r"\btvs?\b|\bcast(ing)?\b|spotify connect", "wearable": r"\bwatch(es)?\b|wearable",
             "desktop-web": r"\bdesktop\b|web player", "background": r"background|screen (is )?off|lock ?screen",
             "offline": r"\boffline\b|downloaded", "app-start": r"\bon (opening|launch)|at (start ?up|launch)|app start"}


FIX_RANGE = (2, 4)    # symptoms in What to fix first
FIX_STEP_WORDS = 25   # one next step, one sentence
_BULLET = re.compile(r"^\s*[-*+]\s")
_ANY_QUOTED = re.compile(r"[\"“]([^\"”\n]+)[\"”]")
_DOUBLE_QUOTES = set('"“”')
# How a rendered bullet reads a stated context or trigger: "<share>% <phrase>".
_PHRASES = {"after-update": "follow an update", "background": "start in the background",
            "bluetooth": "involve Bluetooth", "car": "happen in the car", "tv-cast": "happen while casting",
            "wearable": "involve a watch", "desktop-web": "happen on desktop or web", "offline": "happen offline",
            "app-start": "happen when opening the app"}


def _pct(value: float) -> str:
    """One decimal (the number check tolerates the rounding); two when one decimal would show a non-zero as 0.0."""
    return f"{value:.1f}" if value == 0 or round(value, 1) != 0 else f"{value:.2f}"


FIX_QUOTE_CHARS = 110  # keeps a three-fix memo within the word limit
_TRAILING = {"a", "an", "the", "and", "or", "but", "to", "of", "in", "on", "with", "i", "my", "that", "if", "so"}


def _shorten(quote: str, limit: int = FIX_QUOTE_CHARS) -> str:
    """A leading part of the quote (so still verbatim): whole sentences when they fit, else whole words."""
    if len(quote) <= limit:
        return quote
    head = quote[:limit]
    sentence = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if sentence >= 40:
        return head[:sentence + 1]
    clause = head.rfind(", ")
    if clause >= 0.7 * limit:
        return head[:clause]
    words = head.split(" ")[:-1] or [head]
    while len(words) > 1 and words[-1].lower().strip(",;:-") in _TRAILING:
        words.pop()
    return " ".join(words).rstrip(",;:-")


def _fix_quote(symptom: dict) -> str | None:
    """The most-liked quote that fits whole; else the most-liked one, shortened. Whitespace collapsed, still verbatim."""
    usable = [_SPACES.sub(" ", q["quote"]).strip() for q in symptom.get("quotes", [])
              if not set(q["quote"]) & _DOUBLE_QUOTES]
    whole = next((q for q in usable if len(q) <= FIX_QUOTE_CHARS + 20), None)
    return whole or (_shorten(usable[0]) if usable else None)


def fix_bullet(symptom: dict, next_step: str) -> str:
    """One What-to-fix-first bullet, every number and the quote taken from facts; only the next step is the model's."""
    count = f"{symptom['complaints']:,} complaints"
    if symptom.get("first6_complaints") is not None and symptom.get("last6_complaints") is not None:
        count += (f" ({symptom['first6_complaints']:,} in the first six full months, "
                  f"{symptom['last6_complaints']:,} in the last six)")
    clauses = [f"{count}, {symptom.get('paying_complaints', 0):,} from paying reviewers"]
    for field in ("context", "trigger"):
        value, share = symptom.get(f"top_{field}"), symptom.get(f"top_{field}_share_pct")
        if value and share is not None:
            clauses.append(f"{_pct(share)}% {_PHRASES.get(value, 'mention ' + value)}")
    version = symptom.get("version")
    if version:
        clauses.append(f"{_pct(version['pct_of_this_versions_reviews'])}% of reviews on version "
                       f"{version['app_version']} report it, against {_pct(version['pct_of_all_reviews'])}% "
                       "across all versions")
    line = f"- **{symptom['name']}**: " + "; ".join(clauses) + "."
    quote = _fix_quote(symptom)
    if quote:
        line += f' "{quote}"'
    step = _SPACES.sub(" ", next_step).strip()
    if step and step[-1] not in ".!?":
        step += "."
    return line + f" Next step: {step}"


def render_memo(memo: dict, pack: dict) -> dict:
    """The writer's output with code's What-to-fix-first bullets inserted under that heading.

    `memo_markdown` becomes the rendered memo (what readers, the critic and validate_memo see) and
    `model_markdown` keeps the writer's own text. Each rendered quote joins `quotes` under its symptom id.
    Without a diagnosis in the facts the memo is returned as written.
    """
    out = {**memo, "model_markdown": memo["memo_markdown"], "quotes": list(memo.get("quotes", []))}
    areas = (pack.get("diagnosis") or {}).get("areas") or {}
    if not areas:
        return out
    symptoms = {s["symptom_id"]: s for s in (areas.get(memo.get("priority_area")) or {}).get("symptoms", [])
                if not s.get("plan_gated")}
    bullets, seen = [], set()
    for fix in memo.get("fixes", []):
        symptom = symptoms.get(fix.get("symptom_id"))
        if symptom is None or fix["symptom_id"] in seen:
            continue  # validate_memo reports it
        seen.add(fix["symptom_id"])
        bullets.append(fix_bullet(symptom, fix.get("next_step", "")))
        quote = _fix_quote(symptom)
        if quote and not any(q["quote"] == quote for q in out["quotes"]):
            out["quotes"].append({"issue_id": symptom["symptom_id"], "quote": quote})
    text = memo["memo_markdown"]
    match = re.search(r"^#{1,3} What to fix first[ \t]*$", text, re.M)
    if match:  # keep the writer's framing sentence, drop any bullets it wrote, add code's bullets
        following = _HEADING.search(text, match.end())
        end = following.start() if following else len(text)
        framing = [line for line in text[match.end():end].splitlines() if line.strip() and not _BULLET.match(line)]
        body = "\n".join([*framing, *bullets])
        text = text[:match.end()] + ("\n" + body if body else "") + ("\n\n" + text[end:] if following else "\n")
    out["memo_markdown"] = text
    return out


def _model_output(memo: dict) -> dict:
    """What the writer itself returned (for a revision request): its own markdown, without code's bullets."""
    raw = {k: v for k, v in memo.items() if k != "model_markdown"}
    raw["memo_markdown"] = memo.get("model_markdown", memo["memo_markdown"])
    return raw


def _section(text: str, heading: str) -> str:
    """The body under `## heading`, up to the next heading; empty when the heading is missing."""
    match = re.search(rf"^#{{1,3}} {re.escape(heading)}\s*$", text, re.M)
    if not match:
        return ""
    following = _HEADING.search(text, match.end())
    return text[match.end():following.start() if following else len(text)]


def validate_memo(memo: dict, pack: dict) -> list[str]:
    """Code's check of the writer's output. Returns every problem found; empty means it passed."""
    problems = []
    words = len(memo.get("model_markdown", memo["memo_markdown"]).split())
    if words > MEMO_WORDS:
        problems.append(f"the writer's prose is {words} words; the limit is {MEMO_WORDS} (code's fix bullets are not "
                        "counted), so cut repeated figures")
    missing = [h for h in MEMO_HEADINGS if not re.search(rf"^#{{1,3}} {re.escape(h)}\s*$", memo["memo_markdown"], re.M)]
    if missing:
        problems.append("headings missing or not on their own line as '## Heading': " + ", ".join(missing))
    ranking = {r["issue_id"]: r for r in pack["ranking"]}
    pack_quotes = _pack_quotes(pack)
    evidence = {q["review_id"] for q in pack_quotes}
    claims = {}
    for claim in memo["claims"]:
        cid = claim["claim_id"]
        if cid in claims:
            problems.append(f"{cid}: duplicate claim id")
        claims[cid] = claim
        row = ranking.get(claim["issue_id"])
        if row is None:
            problems.append(f"{cid}: issue {claim['issue_id']} is not in the ranking")
        elif str(row[claim["metric"]]) != claim["value"]:
            problems.append(f"{cid}: {claim['issue_id']} {claim['metric']} is {row[claim['metric']]}, "
                            f"not {claim['value']}")
    text = memo["memo_markdown"]
    cited = set(_CLAIM.findall(text))
    problems += [f"{c}: cited in the memo but missing from claims" for c in sorted(cited - claims.keys())]
    problems += [f"{c}: listed in claims but never cited in the memo" for c in sorted(claims.keys() - cited)]
    if _UUID.search(text):
        problems.append("review ids appear in the memo text; quote the review instead and keep ids out of the prose")
    normal = lambda q: _SPACES.sub(" ", q).strip().lower()
    known = [normal(q["quote"]) for q in pack_quotes]
    for item in memo["quotes"]:
        if not any(normal(item["quote"]) in k for k in known):
            problems.append(f"quote is not verbatim from the evidence pack: {item['quote'][:60]}")
    for quoted in _QUOTED.findall(text):
        if not any(normal(quoted) in k for k in known):
            problems.append(f"quoted text is not verbatim from the evidence pack: {quoted[:60]}")
    for issue in set(m.group(0) for m in _ISSUE.finditer(text)):
        if issue not in ranking:
            problems.append(f"issue {issue} is not in the ranking")
    # An issue-level number stated next to its issue must carry a claim citation in the same sentence.
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if _CLAIM.search(sentence):
            continue
        for issue in set(m.group(0) for m in _ISSUE.finditer(sentence)) & ranking.keys():
            row = ranking[issue]
            values = {str(row[m]) for m in METRICS} | {row["mean_severity"].rstrip("0").rstrip(".")}
            values |= {f"{Decimal(row['mean_severity']):.{d}f}" for d in (1, 2)}
            stated = {n.replace(",", "") for n in _NUMBER.findall(_UUID.sub(" ", sentence))}
            if stated & values:
                problems.append(f"{issue}: issue-level number without a claim citation in: {sentence.strip()[:80]}")
    pack_text = json.dumps(pack)
    # A verbatim customer quote is the customer's words, not a claim: its dates, versions and numbers are not checked.
    unquoted = _ANY_QUOTED.sub(lambda m: " " if any(normal(m.group(1)) in k for k in known) else m.group(0), text)
    for date in set(_DATE.findall(unquoted)):
        if date not in pack_text:
            problems.append(f"date {date} does not appear in the facts")
    known_versions = set(_VERSION.findall(pack_text))
    for version in sorted(set(_VERSION.findall(unquoted)) - known_versions):
        problems.append(f"app version {version} does not appear in the facts")
    diagnosis = (pack.get("diagnosis") or {}).get("areas") or {}
    if diagnosis:
        area = memo.get("priority_area")
        names = [s["name"] for s in (diagnosis.get(area) or {}).get("symptoms", []) if not s.get("plan_gated")]
        section = _section(text, "What to fix first").lower()
        named = sum(name.lower() in section for name in names)
        if named < min(2, len(names)):  # an area with a single diagnosed symptom needs only that one
            problems.append(f"What to fix first names fewer than two symptoms of {area} from facts.diagnosis")
        for name in sorted({s["name"] for a in diagnosis.values() for s in a.get("symptoms", []) if s.get("plan_gated")}):
            if name.lower() in section:
                problems.append(f"{name} is plan-gated: free-tier friction, not a fix")
        # The fixes code rendered: 2-4 distinct, fixable symptoms of the priority area, each with one short step.
        listed = (diagnosis.get(area) or {}).get("symptoms", [])
        by_id = {s["symptom_id"]: s for s in listed}
        fixes = memo.get("fixes") or []
        low, high = min(FIX_RANGE[0], len(names)), FIX_RANGE[1]
        if not low <= len(fixes) <= high:
            problems.append(f"fixes must list {low} to {high} symptoms of {area}; it lists {len(fixes)}")
        seen = set()
        for fix in fixes:
            sid, step = fix.get("symptom_id", ""), fix.get("next_step", "")
            symptom = by_id.get(sid)
            if sid in seen:
                problems.append(f"fix {sid}: listed twice")
                continue
            seen.add(sid)
            if symptom is None:
                problems.append(f"fix {sid}: not a symptom of {area} in facts.diagnosis (use a symptom_id listed there)")
                continue
            if symptom.get("plan_gated"):
                problems.append(f"fix {sid}: {symptom['name']} is plan-gated: free-tier friction, not a fix")
                continue
            words = len(step.split())
            if not words or words > FIX_STEP_WORDS:
                problems.append(f"fix {sid}: next_step is {words} words; write one sentence of at most {FIX_STEP_WORDS}")
            elif re.search(r"[.!?]\s+\S", step.strip()):
                problems.append(f"fix {sid}: next_step must be one sentence")
            # A device or trigger in a next step must be one facts state for that symptom (never one from a quote).
            stated = {symptom.get("top_context"), symptom.get("top_trigger")} - {None}
            for word, pattern in _SETTINGS.items():
                if word not in stated and re.search(pattern, step.lower()):
                    problems.append(f"{symptom['name']}: '{word}' is not a stated context or trigger of this symptom "
                                    "in facts.diagnosis")
    elif memo.get("fixes"):
        problems.append("fixes are listed but facts have no diagnosis; leave fixes empty")
    allowed: set = set()
    _numbers(pack, allowed)
    _numbers([c["value"] for c in memo["claims"]], allowed)
    scrubbed = _VERSION.sub(" ", _DATE.sub(" ", _ISSUE.sub(" ", _UUID.sub(" ", _CLAIM.sub(" ", unquoted)))))
    for raw in set(_NUMBER.findall(scrubbed)):
        number = Decimal(raw.replace(",", ""))
        if number <= 10 and number == number.to_integral_value():
            continue  # ordinals and small counts ("top 3")
        decimals = max(0, -number.as_tuple().exponent)
        tolerance = Decimal(5) / Decimal(10) ** (decimals + 1)
        if not any(abs(number - a) <= tolerance for a in allowed):
            problems.append(f"number {raw} is not supported by the facts")
    return problems


def run_memo(conn, run_dir: Path, writer: Writer, settings) -> dict:
    pack = facts(conn, run_dir, settings)
    key = digest(pack, writer.label_config(MEMO_PROMPT), writer.label_config(CRITIC_PROMPT), CHECKS_VERSION)
    trail = cached(conn, "memo", key)
    from_cache = trail is not None
    if trail is None:
        evidence = sorted({q["review_id"] for q in _pack_quotes(pack)})
        ask = lambda step, prompt_name, extra, schema: writer.ask(
            role="memo", step=step, prompt_name=prompt_name, pack={"facts": pack, **extra}, schema=schema,
            review_ids=evidence)
        write = lambda extra: render_memo(ask("draft" if not extra else "revision", MEMO_PROMPT, extra, MEMO_SCHEMA), pack)
        draft = write({})
        draft_problems = validate_memo(draft, pack)
        critique = ask("critic", CRITIC_PROMPT, {"memo": draft, "code_validation": draft_problems}, CRITIC_SCHEMA)
        trail = {"draft": draft, "draft_problems": draft_problems, "critique": critique, "revised": False}
        final, final_problems, audit = draft, draft_problems, critique
        if draft_problems or critique["verdict"] == "revise":  # one revision; a second only for code checks
            final = write({"previous_draft": _model_output(draft), "code_validation": draft_problems,
                           "critic": critique["problems"]})
            final_problems = validate_memo(final, pack)
            audit = ask("critic", CRITIC_PROMPT, {"memo": final, "code_validation": final_problems}, CRITIC_SCHEMA)
            trail.update({"revised": True, "revision": final})
            if final_problems:  # code checks still fail: one more revision, for them alone
                previous = final
                final = write({"previous_draft": _model_output(previous), "code_validation": final_problems,
                               "critic": []})
                final_problems = validate_memo(final, pack)
                audit = ask("critic", CRITIC_PROMPT, {"memo": final, "code_validation": final_problems},
                            CRITIC_SCHEMA)
                trail.update({"revised_twice": True, "revision2": final})
        trail.update({"final": final, "final_problems": final_problems, "final_critique": audit})
        remember(conn, "memo", key, trail)
    final = trail["final"]
    bad = {p.split(":")[0] for p in trail["final_problems"] if p.startswith("C")}
    claims = [c for c in final["claims"] if c["claim_id"] not in bad]
    with (run_dir / "claims.csv").open("w", encoding="utf-8", newline="") as f:
        out = csv.writer(f, lineterminator="\n")
        out.writerow(["claim_id", "issue_id", "metric", "value"])
        out.writerows([c["claim_id"], c["issue_id"], c["metric"], c["value"]] for c in claims)
    lines = [f"# {final['headline']}", "", f"**Recommended priority: {final['priority_area']}**", "",
             final["memo_markdown"].strip(), "", "## Claims (checked by code against `ranking.csv`)", "",
             "| Claim | Issue | Metric | Value |", "|---|---|---|---:|"]
    lines += [f"| {c['claim_id']} | `{c['issue_id']}` | {c['metric']} | {c['value']} |" for c in claims]
    lines += ["", "## Review trail", "",
              f"- Critic on the first draft: **{trail['critique']['verdict']}**"
              + (f" ({'; '.join(trail['critique']['problems'])})" if trail["critique"]["problems"] else ""),
              f"- Revised: {'yes, twice (the second for code checks)' if trail.get('revised_twice') else 'yes, once' if trail['revised'] else 'no'}",
              f"- Critic on the final memo: **{trail['final_critique']['verdict']}**",
              "- Unresolved code-check problems: " + ("; ".join(trail["final_problems"]) or "none"), ""]
    (run_dir / "memo.md").write_text("\n".join(lines), encoding="utf-8")
    trail["final_quote_sources"] = quote_sources(final, pack)
    (run_dir / "memo.json").write_text(json.dumps({"facts": pack, **trail}, indent=2, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
    return {"priority_area": final["priority_area"], "claims": len(claims), "revised": trail["revised"],
            "final_problems": trail["final_problems"], "from_cache": from_cache}
