"""Local hand-labelling UI for the 50 golden reviews. Standard library only.

    python3.12 evals/golden_ui.py                 # opens http://127.0.0.1:8765/
    python3.12 evals/golden_ui.py --no-browser --port 8766 --out /tmp/try.csv

Every label comes from the human using the page. The tool never calls a model and
never shows a prediction. Topic, intent, sentiment and severity always start empty; the
only default is the evidence box, which starts as the whole stripped text of a short
review. Label columns are read only from the tool's own output file (never from the
input), so restarting resumes earlier work.

Output: the 13 input columns (the six source fields written back unchanged), then
ambiguous, alt_topic, alt_intent, alt_severity, notes, labeled_at, then the two
"free vs premium" columns free_tier_limit and plan. Those two may be blank (not yet
labelled); an older file without them loads with them blank and gains them on the
next save. Each save validates the label and rewrites the CSV atomically
(temp file + os.replace).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import tempfile
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "golden_50_to_label.csv"
DEFAULT_OUT = ROOT / "evals" / "golden_50_labeled.csv"
HTML_PATH = Path(__file__).resolve().with_name("golden_ui.html")
HOST = "127.0.0.1"

SOURCE_FIELDS = ["review_id", "review_text", "review_rating", "review_likes", "app_version", "review_timestamp"]
LABEL_FIELDS = ["topic", "intent", "sentiment", "severity", "entities", "evidence_quote", "needs_review"]
EXTRA_FIELDS = ["ambiguous", "alt_topic", "alt_intent", "alt_severity", "notes", "labeled_at"]
PLAN_FIELDS = ["free_tier_limit", "plan"]  # added later; blank means "not yet labelled for these two"
INPUT_FIELDS = SOURCE_FIELDS + LABEL_FIELDS
OUTPUT_FIELDS = INPUT_FIELDS + EXTRA_FIELDS + PLAN_FIELDS
HUMAN_FIELDS = LABEL_FIELDS + EXTRA_FIELDS[:-1] + PLAN_FIELDS  # everything the human sets; labeled_at is set by the server
METADATA_FIELDS = SOURCE_FIELDS[2:]

# Definitions copied from data/GRADING_CONTRACT.md ("Common labels for fair comparison") and prompts/labels.md.
TOPICS = [
    ("access", "Login, signup, password or account access"),
    ("usability", "Navigation, controls, layout, queue/playlist management, ad interruptions"),
    ("playback", "Playback failure, crashes, lag, connection failures, audio quality, resource use"),
    ("downloads", "Downloading, saved music, offline listening, disappearing downloads"),
    ("catalog", "Missing songs/artists, search/discovery, recommendations, lyrics availability"),
    ("billing", "Price, charges, subscriptions, paywalls, premium entitlement; explicitly premium-only controls go here"),
    ("support", "Contacting support and the support response"),
    ("other", "General praise/criticism, unrelated content, or no supported specific topic"),
]
INTENTS = [
    ("cancellation", "Explicitly leaving, uninstalling, cancelling, switching away, or threatening to do so"),
    ("complaint", "Negative experience, including mixed praise/criticism. General “bad app” is a complaint"),
    ("request", "Desired change without a reported failure"),
    ("praise", "Praise"),
    ("unclear", "Meaningless or unrelated text, or a bare boycott slogan with no product complaint or personal departure"),
]
SEVERITIES = [
    (1, "No reported problem: praise, neutral/unclear content, or a pure feature request"),
    (2, "Dislike, generic criticism, minor annoyance, or a cosmetic issue; no supported functional loss"),
    (3, "A degraded or restricted function; some use or workaround remains"),
    (4, "A clearly blocked core task, such as inability to log in or play music"),
    (5, "Explicit serious financial, privacy, or data harm; an expensive plan, a crash, or angry language alone is insufficient"),
]
RULES = {
    "topic": [
        "Choose the problem with the highest supported severity. On a tie, choose the first specific problem mentioned.",
        "For a positive review, choose the first specific praised feature; general praise is other.",
        "Mentioning a paid plan alone does not make the topic billing. A subscription failing to activate does; music crashing for a paying customer is playback.",
        "“Bad app” with no detail is other. Never infer a defect the review does not state.",
    ],
    "intent": [
        "First match wins: cancellation → complaint → request → praise → unclear.",
        "Bare boycott slogans and unrelated/meaningless text are unclear, unless there is a product complaint or explicit personal departure.",
    ],
    "severity": [
        "The reported impact, not the star rating, the anger, or the intent.",
        "Cancellation intent does not automatically raise severity. If the impact is not stated, do not invent it.",
    ],
    "severity_examples": [
        ["Best music app ever", 1],
        ["Please add a sleep timer on the home screen", 1],
        ["Too many ads, so annoying", 2],
        ["The new layout is ugly", 2],
        ["Songs keep pausing every few minutes, I have to press play again", 3],
        ["Free users can only shuffle now, I can't pick the song I want", 3],
        ["App closes the moment I open it, can't listen to anything", 4],
        ["Won't let me log in since the update", 4],
        ["Charged me three times this month and no refund", 5],
        ["Someone got into my account and my card details were used", 5],
    ],
    "needs_review": "Missing context should trigger needs_review; do not invent impact. "
    "If you do not understand the language, use other, unclear, severity 1 and set needs_review.",
    "metadata": "Classify the review text itself. Keep stars and other metadata for analysis; "
    "do not use them to substitute for interpreting the text.",
}
FREE_TIER_LIMIT = (
    "Does the review complain about something the free version restricts by design: ads between songs, "
    "shuffle-only play, a skip limit, not being able to pick a song or play in order, seeking, or a feature "
    "locked to premium (downloads, lyrics, etc.)? Bugs, crashes, redesigns and catalog gaps are not free-tier "
    "limits. Reviews with no complaint are false."
)
PLANS = [
    ("paid", "Says they pay or have premium, family, duo or student"),
    ("free", "Says they use the free version or clearly says they don't pay"),
    ("unstated", "Does not say"),
]
PLAN_DEFINITION = (
    "What does the reviewer say about their own plan? paid = says they pay or have premium, family, duo or "
    "student; free = says they use the free version or clearly says they don't pay; unstated = does not say. "
    "A complaint about premium-only features alone does not reveal the plan."
)
PLAN_NAMES = [name for name, _ in PLANS]
TOPIC_NAMES = [name for name, _ in TOPICS]
INTENT_NAMES = [name for name, _ in INTENTS]
SHORT_REVIEW_CHARS = 140  # the evidence box defaults to the whole stripped text up to this length
MAX_ENTITIES = 12
MAX_ENTITY_CHARS = 40
MAX_NOTES_CHARS = 2000
MAX_BODY_BYTES = 1_000_000


class SetupError(Exception):
    """A problem with the input or output file that must stop the tool before it writes anything."""


# ---------------------------------------------------------------- files

def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        rows = [row for row in reader]
    if header is None:
        raise SetupError(f"{path} is empty")
    return header, rows


def read_source(path: Path) -> list[dict[str, str]]:
    """Return the input rows as dicts of the six source fields, exactly as stored."""
    header, rows = read_csv(path)
    if header != INPUT_FIELDS:
        raise SetupError(f"{path}: expected columns {INPUT_FIELDS}, found {header}")
    out, seen = [], set()
    for n, row in enumerate(rows, start=2):
        if len(row) != len(header):
            raise SetupError(f"{path} row {n}: expected {len(header)} fields, found {len(row)}")
        record = dict(zip(header, row))
        if not record["review_id"] or record["review_id"] in seen:
            raise SetupError(f"{path} row {n}: missing or repeated review_id")
        seen.add(record["review_id"])
        out.append({k: record[k] for k in SOURCE_FIELDS})
    return out


def load_existing(out_path: Path, source: list[dict[str, str]]) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Read human labels back from an earlier output file. Returns (labels by review_id, warnings)."""
    labels: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    if not out_path.exists():
        return labels, warnings
    header, rows = read_csv(out_path)
    unknown = [c for c in header if c not in OUTPUT_FIELDS]
    missing = [c for c in ["review_id"] + LABEL_FIELDS if c not in header]
    if unknown or missing:
        raise SetupError(
            f"{out_path} does not look like a golden label file (unknown columns {unknown}, missing {missing}); "
            "refusing to overwrite it. Move it away or pass a different --out."
        )
    by_id = {r["review_id"]: r for r in source}
    for n, row in enumerate(rows, start=2):
        if len(row) != len(header):
            raise SetupError(f"{out_path} row {n}: expected {len(header)} fields, found {len(row)}; refusing to overwrite it")
        record = dict(zip(header, row))
        rid = record["review_id"]
        if rid not in by_id:
            raise SetupError(f"{out_path} row {n}: review_id {rid!r} is not in the input; refusing to overwrite it")
        if rid in labels:
            warnings.append(f"review {rid} appears twice in {out_path.name}; keeping the last row")
        changed = [f for f in SOURCE_FIELDS if f in record and record[f] != by_id[rid][f]]
        if changed:
            warnings.append(f"review {rid}: {', '.join(changed)} differ from the input; the input values will be written back")
        label = {f: record.get(f, "") for f in HUMAN_FIELDS + ["labeled_at"]}
        if any(label[f] for f in HUMAN_FIELDS):
            labels[rid] = label
    return labels, warnings


def write_output(out_path: Path, source: list[dict[str, str]], labels: dict[str, dict[str, str]]) -> None:
    """Write every review (labelled or blank) to out_path atomically."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_path.parent, prefix=f".{out_path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(OUTPUT_FIELDS)
            for record in source:
                label = labels.get(record["review_id"], {})
                writer.writerow([record[k] for k in SOURCE_FIELDS] + [label.get(k, "") for k in OUTPUT_FIELDS[6:]])
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates the file as 0600; keep the existing file's mode, or use an ordinary 0644.
        os.chmod(tmp, out_path.stat().st_mode & 0o777 if out_path.exists() else 0o644)
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------- validation

def _bool(value, field: str, errors: dict[str, str]) -> str:
    if value is None:
        return "false"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value in ("true", "false"):
        return value
    errors[field] = f"{field} must be true or false"
    return ""


def _optional_bool(value, field: str, errors: dict[str, str]) -> str:
    """Like _bool, but blank stays blank (not yet labelled) instead of becoming false."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value in ("true", "false"):
        return value
    errors[field] = f"{field} must be true, false or blank"
    return ""


def _severity(value, field: str, errors: dict[str, str], *, required: bool) -> str:
    if value is None or value == "":
        if required:
            errors[field] = "choose a severity from 1 to 5"
        return ""
    number = None
    if isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif isinstance(value, str) and value.strip() in ("1", "2", "3", "4", "5"):
        number = int(value.strip())
    if number is None or not 1 <= number <= 5:
        errors[field] = f"{field} must be a whole number from 1 to 5"
        return ""
    return str(number)


def _choice(value, field: str, allowed: list[str], errors: dict[str, str], *, required: bool) -> str:
    if value is None or value == "":
        if required:
            errors[field] = f"choose a {field}"
        return ""
    if value not in allowed:
        errors[field] = f"{field} must be one of: {', '.join(allowed)}"
        return ""
    return value


def _sentiment(value, errors: dict[str, str]) -> str:
    if value is None or value == "":
        errors["sentiment"] = "set a sentiment from -1 to 1"
        return ""
    number = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            number = None
    if number is None or not math.isfinite(number) or not -1 <= number <= 1:
        errors["sentiment"] = "sentiment must be a number from -1 to 1"
        return ""
    return str(round(number, 2) + 0.0)


def _entities(value, errors: dict[str, str]) -> str:
    if value is None or value == "":
        items = []
    elif isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        items = value
    else:
        errors["entities"] = "entities must be a list of short terms"
        return ""
    cleaned, seen = [], set()
    for item in items:
        term = " ".join(item.split())
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            cleaned.append(term)
    if len(cleaned) > MAX_ENTITIES:
        errors["entities"] = f"at most {MAX_ENTITIES} entities"
    elif any(len(t) > MAX_ENTITY_CHARS for t in cleaned):
        errors["entities"] = f"each entity must be a short term (at most {MAX_ENTITY_CHARS} characters)"
    return json.dumps(cleaned, ensure_ascii=False)


def validate_label(payload, review_text: str) -> tuple[dict[str, str] | None, dict[str, str]]:
    """Check one human label against the shared definitions.

    Returns (fields as CSV strings, {}) when valid, or (None, {field: message}).
    The evidence quote must be an exact substring of review_text; it is not stripped or normalised.
    """
    if not isinstance(payload, dict):
        return None, {"_": "the label must be a JSON object"}
    errors: dict[str, str] = {}
    unknown = sorted(set(payload) - set(HUMAN_FIELDS) - {"review_id"})
    if unknown:
        errors["_"] = f"unknown field(s): {', '.join(unknown)}"

    out = {
        "topic": _choice(payload.get("topic"), "topic", TOPIC_NAMES, errors, required=True),
        "intent": _choice(payload.get("intent"), "intent", INTENT_NAMES, errors, required=True),
        "sentiment": _sentiment(payload.get("sentiment"), errors),
        "severity": _severity(payload.get("severity"), "severity", errors, required=True),
        "entities": _entities(payload.get("entities"), errors),
        "needs_review": _bool(payload.get("needs_review"), "needs_review", errors),
        "ambiguous": _bool(payload.get("ambiguous"), "ambiguous", errors),
        "alt_topic": _choice(payload.get("alt_topic"), "alt_topic", TOPIC_NAMES, errors, required=False),
        "alt_intent": _choice(payload.get("alt_intent"), "alt_intent", INTENT_NAMES, errors, required=False),
        "alt_severity": _severity(payload.get("alt_severity"), "alt_severity", errors, required=False),
        "free_tier_limit": _optional_bool(payload.get("free_tier_limit"), "free_tier_limit", errors),
        "plan": _choice(payload.get("plan"), "plan", PLAN_NAMES, errors, required=False),
    }

    quote = payload.get("evidence_quote")
    if not isinstance(quote, str) or not quote.strip():
        errors["evidence_quote"] = "add an evidence quote copied from the review"
    elif quote not in review_text:
        errors["evidence_quote"] = "the evidence quote must be an exact substring of the review text"
    out["evidence_quote"] = quote if isinstance(quote, str) else ""

    notes = payload.get("notes")
    if notes is None:
        notes = ""
    if not isinstance(notes, str):
        errors["notes"] = "notes must be text"
        notes = ""
    elif len(notes) > MAX_NOTES_CHARS:
        errors["notes"] = f"notes must be at most {MAX_NOTES_CHARS} characters"
    out["notes"] = notes.strip()

    for alt, main in (("alt_topic", "topic"), ("alt_intent", "intent"), ("alt_severity", "severity")):
        if out[alt] and out[alt] == out[main] and alt not in errors:
            errors[alt] = f"the alternative must differ from the chosen {main}"
    if any(out[a] for a in ("alt_topic", "alt_intent", "alt_severity")) and out["ambiguous"] == "false":
        errors.setdefault("ambiguous", "tick ambiguous when you give an alternative label")

    if errors:
        return None, errors
    return out, {}


def stored_to_payload(stored: dict[str, str]) -> dict:
    """Turn the CSV strings of a saved label back into the JSON shape the page and validator use."""
    payload: dict = {}
    for field in HUMAN_FIELDS:
        value = stored.get(field, "")
        if field in ("needs_review", "ambiguous"):
            value = {"true": True, "false": False, "": False}.get(value, value)
        elif field == "free_tier_limit":
            value = {"true": True, "false": False}.get(value, value)  # blank stays blank
        elif field in ("severity", "alt_severity") and value.strip() in ("1", "2", "3", "4", "5"):
            value = int(value)
        elif field == "sentiment" and value:
            try:
                value = float(value)
            except ValueError:
                pass
        elif field == "entities":
            try:
                parsed = json.loads(value) if value else []
                value = parsed if isinstance(parsed, list) else value
            except ValueError:
                pass
        payload[field] = value
    return payload


# ---------------------------------------------------------------- state

class LabelStore:
    """The input rows plus the human labels, saved to out_path after every change."""

    def __init__(self, input_path: Path, out_path: Path):
        self.input_path = Path(input_path)
        self.out_path = Path(out_path)
        if self.input_path.resolve() == self.out_path.resolve():
            raise SetupError("--out must not be the input file")
        self.source = read_source(self.input_path)
        self.by_id = {r["review_id"]: r for r in self.source}
        self.labels, self.warnings = load_existing(self.out_path, self.source)
        self.lock = threading.Lock()

    def status(self, review_id: str) -> tuple[str, dict[str, str]]:
        stored = self.labels.get(review_id)
        if not stored:
            return "todo", {}
        _, errors = validate_label(stored_to_payload(stored), self.by_id[review_id]["review_text"])
        return ("invalid" if errors else "done"), errors

    def done_count(self) -> int:
        return sum(self.status(r["review_id"])[0] == "done" for r in self.source)

    def plan_filled(self, review_id: str) -> bool:
        """True when both free-vs-premium fields hold an allowed value."""
        stored = self.labels.get(review_id) or {}
        return stored.get("free_tier_limit", "") in ("true", "false") and stored.get("plan", "") in PLAN_NAMES

    def plan_done_count(self) -> int:
        return sum(self.plan_filled(r["review_id"]) for r in self.source)

    def snapshot(self) -> dict:
        with self.lock:
            reviews = []
            for i, r in enumerate(self.source):
                status, errors = self.status(r["review_id"])
                stored = self.labels.get(r["review_id"])
                reviews.append({
                    "index": i,
                    "review_id": r["review_id"],
                    "review_text": r["review_text"],
                    "metadata": {k: r[k] for k in METADATA_FIELDS},
                    "status": status,
                    "errors": errors,
                    "plan_done": self.plan_filled(r["review_id"]),
                    "label": stored_to_payload(stored) if stored else None,
                    "labeled_at": stored.get("labeled_at", "") if stored else "",
                })
            return {
                "reviews": reviews,
                "done": sum(r["status"] == "done" for r in reviews),
                "plan_done": sum(r["plan_done"] for r in reviews),
                "total": len(reviews),
                "out": str(self.out_path),
                "schema": {
                    "topics": [{"value": v, "definition": d} for v, d in TOPICS],
                    "intents": [{"value": v, "definition": d} for v, d in INTENTS],
                    "severities": [{"value": v, "definition": d} for v, d in SEVERITIES],
                    "rules": RULES,
                    "short_review_chars": SHORT_REVIEW_CHARS,
                    "free_tier_limit": FREE_TIER_LIMIT,
                    "plans": [{"value": v, "definition": d} for v, d in PLANS],
                    "plan_definition": PLAN_DEFINITION,
                },
            }

    def save(self, payload) -> tuple[bool, dict]:
        rid = payload.get("review_id") if isinstance(payload, dict) else None
        if not isinstance(rid, str) or rid not in self.by_id:
            return False, {"errors": {"_": "unknown review_id"}}
        fields, errors = validate_label(payload, self.by_id[rid]["review_text"])
        if errors:
            return False, {"errors": errors}
        fields["labeled_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            previous = self.labels.get(rid)
            self.labels[rid] = fields
            try:
                write_output(self.out_path, self.source, self.labels)
            except BaseException:
                self._restore(rid, previous)
                raise
            done = self.done_count()
            plan_done = self.plan_done_count()
        return True, {"label": stored_to_payload(fields), "labeled_at": fields["labeled_at"], "status": "done", "done": done,
                      "plan_done": plan_done, "review_plan_done": self.plan_filled(rid)}

    def clear(self, review_id) -> tuple[bool, dict]:
        if not isinstance(review_id, str) or review_id not in self.by_id:
            return False, {"errors": {"_": "unknown review_id"}}
        with self.lock:
            previous = self.labels.pop(review_id, None)
            try:
                write_output(self.out_path, self.source, self.labels)
            except BaseException:
                self._restore(review_id, previous)
                raise
            done = self.done_count()
            plan_done = self.plan_done_count()
        return True, {"status": "todo", "done": done, "plan_done": plan_done}

    def _restore(self, rid: str, previous: dict[str, str] | None) -> None:
        if previous is None:
            self.labels.pop(rid, None)
        else:
            self.labels[rid] = previous


# ---------------------------------------------------------------- HTTP

CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
    "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def make_handler(store: LabelStore, html_path: Path = HTML_PATH):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GoldenUI/1"

        def log_message(self, fmt, *args):  # keep the terminal quiet; saves are printed below
            pass

        def _allowed_host(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: dict) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            if not self._allowed_host():
                return self._json(403, {"ok": False, "errors": {"_": "forbidden host"}})
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._send(200, html_path.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/reviews":
                return self._json(200, store.snapshot())
            return self._json(404, {"ok": False, "errors": {"_": "not found"}})

        def do_POST(self):
            port = self.server.server_address[1]
            origin = self.headers.get("Origin")
            if not self._allowed_host() or (origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")):
                return self._json(403, {"ok": False, "errors": {"_": "forbidden origin"}})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json(415, {"ok": False, "errors": {"_": "send JSON (Content-Type: application/json)"}})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY_BYTES:
                return self._json(400, {"ok": False, "errors": {"_": "missing or oversized body"}})
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return self._json(400, {"ok": False, "errors": {"_": "body is not valid JSON"}})
            path = self.path.split("?", 1)[0]
            if path == "/api/label":
                ok, result = store.save(payload)
                if ok:
                    print(f"saved {payload['review_id']}  ({result['done']}/{len(store.source)} labelled, "
                          f"plan fields {result['plan_done']}/{len(store.source)})", flush=True)
            elif path == "/api/clear":
                rid = payload.get("review_id") if isinstance(payload, dict) else None
                ok, result = store.clear(rid)
                if ok:
                    print(f"cleared {rid}  ({result['done']}/{len(store.source)} labelled)", flush=True)
            else:
                return self._json(404, {"ok": False, "errors": {"_": "not found"}})
            return self._json(200 if ok else 400, {"ok": ok, **result})

    return Handler


def make_server(store: LabelStore, port: int, html_path: Path = HTML_PATH) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, port), make_handler(store, html_path))
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hand-label the 50 golden reviews in a local web page.")
    parser.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="labelled CSV (default evals/golden_50_labeled.csv)")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true", help="do not open the page in a browser")
    args = parser.parse_args(argv)

    try:
        store = LabelStore(args.input, args.out)
    except (SetupError, OSError) as exc:
        print(f"golden_ui: {exc}", file=sys.stderr)
        return 2
    for warning in store.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    try:
        server = make_server(store, args.port)
    except OSError as exc:
        print(f"golden_ui: cannot listen on {HOST}:{args.port} ({exc}); try --port", file=sys.stderr)
        return 2

    url = f"http://{HOST}:{server.server_address[1]}/"
    state = "file exists, resuming" if store.out_path.exists() else "created on first save"
    print(f"Golden labelling UI: {url}")
    print(f"{store.done_count()}/{len(store.source)} labelled, plan fields {store.plan_done_count()}/{len(store.source)}. Saving to {store.out_path} ({state}).")
    print("Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
