"""A keyword-rule stand-in for a model, for offline plumbing tests only.

Its labels are not classifications and its token counts are not usage. Nothing it produces
may be reported as a result; its tier is `synthetic` and its rates are zero.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from ..config import CONTEXT_CODES, INTENT_CODES, TOPIC_CODES
from ..textproc import entities
from .base import ModelRequest, ModelResponse, Provider

_LINE = re.compile(r"^(\d+): (.*)$")
_DIAGNOSE_LINE = re.compile(r"^(\d+) \[(ac|us|pb|bs)\] (.*)$")
_CATALOG_AREA = re.compile(r"^(ac|us|pb|bs) = ")
_CATALOG_ITEM = re.compile(r"^  (\d+) (.*)$")
_CONTEXTS = [("B", r"bluetooth|headphone|earbud|speaker"), ("C", r"\bcar\b|android auto"),
             ("T", r"\btv\b|chromecast|\bcast|spotify connect|alexa|google home"), ("W", r"\bwatch\b|wear ?os"),
             ("D", r"desktop|laptop|\bpc\b|web player|computer")]
_TRIGGERS = [("u", r"update"), ("s", r"screen (?:is )?off|background|lock ?screen|minimi"),
             ("o", r"offline|download"), ("k", r"\bopen|launch|start")]
_LONG_TIME = r"\byears\b|since 20\d\d|loyal|long[- ]time"
_STOP = {"with", "when", "that", "this", "from", "have", "after", "does", "doesn", "their", "they", "about", "into",
         "other", "problem", "problems", "spotify", "music", "songs", "song", "review", "reviews", "complaint"}
# A synthetic symptom catalog for the fake discovery step: plain keywords so the fake diagnoser can match them.
_FAKE_SYMPTOMS = {
    "access": [("login-fails", "Login fails", "Cannot log in or sign in with a password or code"),
               ("logged-out", "Logged out", "The app logs the user out of the account"),
               ("wont-open", "App will not open", "The app does not open or start at all"),
               ("account-locked", "Account locked", "The account is locked, banned or deleted")],
    "usability": [("ads-too-many", "Too many ads", "Complaints about the number or length of ads"),
                  ("layout-changed", "Layout changed", "The interface, layout or design changed for the worse"),
                  ("playlist-hard", "Playlist management hard", "Hard to edit, sort or find a playlist or the queue"),
                  ("controls-broken", "Controls broken", "Buttons, skip or shuffle controls do not respond")],
    "playback": [("stops-playing", "Music stops playing", "Playback stops, pauses or cuts out by itself"),
                 ("app-crashes", "App crashes", "The app crashes or freezes while playing"),
                 ("buffering-slow", "Buffering and loading", "Songs buffer, lag or load slowly"),
                 ("sound-quality", "Sound quality", "Audio quality is poor, quiet or distorted")],
    "billing/support": [("charged-wrongly", "Charged wrongly", "Charged, billed or a payment taken unexpectedly"),
                        ("price-too-high", "Price too high", "The premium price or subscription costs too much"),
                        ("cannot-cancel", "Cannot cancel", "Cannot cancel the subscription or get a refund"),
                        ("support-unhelpful", "Support unhelpful", "Customer support does not help or answer")],
}
_SEGMENT = re.compile(r"\[(\d+)\] ")
_TOPICS = [
    ("ac", r"log ?in|sign ?in|password|account"),
    ("dl", r"download|offline"),
    ("bi", r"premium|pay|price|charge|subscri|refund|money"),
    ("su", r"customer (?:service|support)|support team"),
    ("pb", r"crash|stop|pause|lag|buffer|won'?t play|not play|freez|sound quality"),
    ("ca", r"lyrics|search|recommend|song.{0,12}missing|podcast"),
    ("us", r"\bads?\b|shuffle|skip|layout|update|queue|playlist|interface"),
]
_LEAVING = r"uninstall|cancel|unsubscrib|switch(?:ing|ed)? to|delet(?:e|ing) (?:the|this) app"
_BLOCKED = r"can'?t|cannot|won'?t|unable|not work|doesn'?t work|crash"
_DEGRADED = r"keeps?|sometimes|slow|lag|only|limited|stops?|pause"
_NEGATIVE = r"bad|worst|hate|annoy|terrible|horrible|useless|trash|poor|boring|problem|issue|too many"
_POSITIVE = r"good|great|love|best|nice|awesome|amazing|excellent|super|perfect|fantastic"
_REQUEST = r"please add|wish|would be nice|should add|bring back"


def _label(n: int, text: str, step: str) -> str:
    low = text.lower()
    has = lambda pattern: re.search(pattern, low) is not None
    topic = next((code for code, pattern in _TOPICS if has(pattern)), "ot")
    problem = has(_BLOCKED) or has(_DEGRADED) or has(_NEGATIVE)
    if has(_LEAVING):
        intent = "x"
    elif problem:
        intent = "c"
    elif has(_REQUEST):
        intent = "r"
    elif has(_POSITIVE):
        intent = "p"
    else:
        intent = "u"
    severity = 1 if intent in "rpu" or not problem else 4 if has(_BLOCKED) else 3 if has(_DEGRADED) else 2
    sentiment = {"x": -7, "c": -5, "r": 0, "p": 8, "u": 0}[intent]
    quote = 0
    parts = _SEGMENT.split(text)
    if len(parts) > 1:
        for number, body in zip(parts[1::2], parts[2::2]):
            if re.search(f"{_BLOCKED}|{_DEGRADED}|{_NEGATIVE}", body.lower()):
                quote = int(number)
                break
    letters = sum(ch.isascii() and ch.isalpha() for ch in text)
    unsure = letters < max(3, len(text) // 2)
    if step == "primary" and intent == "u" and len(low.split()) > 3:
        unsure = True
    limit = "y" if intent in "xcr" and has(r"\bads?\b|shuffle|skip|premium only|need premium|have to pay") else "n"
    plan = "p" if has(r"\bi pay\b|premium user|i have premium|my premium") else "f" if has(r"free (version|user)") else "u"
    return f"{n}|{topic}|0|{intent}|{severity}|{sentiment}|{quote}|{int(unsure)}|{limit}|{plan}"


class FakeProvider(Provider):
    name = "fake"
    sync_tier = "synthetic"
    batch_tier = "synthetic"

    def __init__(self, model: str = "fake-rules-v0", spool: Path | None = None, *, bad_marker: str | None = None,
                 bad_once_marker: str | None = None, refuse_marker: str | None = None, fail_first: int = 0,
                 interrupt_after: int | None = None, batch_polls: int = 1):
        self.model = model
        self.spool = Path(spool) if spool else None
        self.bad_marker, self.bad_once_marker, self.refuse_marker = bad_marker, bad_once_marker, refuse_marker
        self.fail_first, self.interrupt_after, self.batch_polls = fail_first, interrupt_after, batch_polls
        self.calls = 0
        self._seen_once: set[str] = set()

    def _answer(self, request: ModelRequest) -> ModelResponse:
        started = time.time()
        if request.step == "diagnose":
            return self._diagnose(request, started)
        if request.schema:  # group and memo roles: a deterministic JSON answer built from the pack
            output = json.dumps(_json_answer(request))
            return ModelResponse(request_id=f"fake-{uuid.uuid4().hex}", ok=True, text=output,
                                 input_tokens=(len(request.system) + len(request.user)) // 4,
                                 output_tokens=len(output) // 3, usage_available=False, stop_reason="end_turn",
                                 started_at=started, duration_ms=int((time.time() - started) * 1000))
        lines, stop = [], "end_turn"
        for raw in request.user.splitlines():
            match = _LINE.match(raw)
            if not match:
                continue
            n, text = int(match.group(1)), match.group(2)
            if self.refuse_marker and self.refuse_marker in text:  # a refusal ends the output mid-way
                stop = "refusal"
                break
            if self.bad_marker and self.bad_marker in text:
                lines.append(f"{n}|this line is malformed")
            elif self.bad_once_marker and self.bad_once_marker in text and text not in self._seen_once:
                self._seen_once.add(text)
            else:
                lines.append(_label(n, text, request.step))
        output = "\n".join(lines)
        return ModelResponse(
            request_id=f"fake-{uuid.uuid4().hex}", ok=True, text=output,
            input_tokens=(len(request.system) + len(request.user)) // 4, output_tokens=len(output) // 3,
            usage_available=False, stop_reason=stop, refusal_category="general_harms" if stop == "refusal" else None,
            started_at=started, duration_ms=int((time.time() - started) * 1000))

    def _diagnose(self, request: ModelRequest, started: float) -> ModelResponse:
        catalog = _catalog_from_prompt(request.system)
        lines, stop = [], "end_turn"
        for raw in request.user.splitlines():
            match = _DIAGNOSE_LINE.match(raw)
            if not match:
                continue
            n, code, text = int(match.group(1)), match.group(2), match.group(3)
            if self.refuse_marker and self.refuse_marker in text:
                stop = "refusal"
                break
            if self.bad_marker and self.bad_marker in text:
                lines.append(f"{n}|this line is malformed")
            elif self.bad_once_marker and self.bad_once_marker in text and text not in self._seen_once:
                self._seen_once.add(text)
            else:
                s, c, t, y, q = diagnose_fields(text, catalog.get(code, []))
                lines.append(f"{n}|{s}|{c}|{t}|{y}|{q}")
        output = "\n".join(lines)
        return ModelResponse(
            request_id=f"fake-{uuid.uuid4().hex}", ok=True, text=output,
            input_tokens=(len(request.system) + len(request.user)) // 4, output_tokens=len(output) // 3,
            usage_available=False, stop_reason=stop, refusal_category="general_harms" if stop == "refusal" else None,
            started_at=started, duration_ms=int((time.time() - started) * 1000))

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.interrupt_after is not None and self.calls > self.interrupt_after:
            raise KeyboardInterrupt("fake provider: simulated interruption")
        if self.calls <= self.fail_first:
            return ModelResponse(request_id=f"fake-{uuid.uuid4().hex}", ok=False, error="simulated_503",
                                 usage_available=False, started_at=time.time())
        return self._answer(request)

    # A file-backed imitation of an asynchronous batch, so resume can be tested across processes.
    def submit_batch(self, requests: list[ModelRequest]) -> str:
        batch_id = f"fakebatch-{uuid.uuid4().hex[:12]}"
        self.spool.mkdir(parents=True, exist_ok=True)
        (self.spool / f"{batch_id}.json").write_text(
            json.dumps({"polls": 0, "requests": [asdict(r) for r in requests]}), encoding="utf-8")
        return batch_id

    def poll_batch(self, provider_batch: str) -> str:
        path = self.spool / f"{provider_batch}.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["polls"] += 1
        path.write_text(json.dumps(state), encoding="utf-8")
        return "completed" if state["polls"] >= self.batch_polls else "in_progress"

    def fetch_batch(self, provider_batch: str) -> dict[str, ModelResponse]:
        state = json.loads((self.spool / f"{provider_batch}.json").read_text(encoding="utf-8"))
        return {r["key"]: self._answer(ModelRequest(**r)) for r in state["requests"]}


def _catalog_from_prompt(system: str) -> dict[str, list[str]]:
    """Read the numbered symptom list back out of the diagnose prompt: area code -> 'name: definition' lines."""
    catalog, area = {}, None
    for line in system.splitlines():
        if _CATALOG_AREA.match(line):
            area = line[:2]
            catalog[area] = []
        elif area and _CATALOG_ITEM.match(line):
            catalog[area].append(_CATALOG_ITEM.match(line).group(2))
        elif area and not line.startswith("  "):
            area = None
    return catalog


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP}


def diagnose_fields(text: str, symptoms: list[str]) -> tuple[int, str, str, str, int]:
    """Keyword rules: the symptom sharing most words with the text (0 if none), then context, trigger, years."""
    low = text.lower()
    parts = _SEGMENT.split(text)
    segs = parts[2::2] if len(parts) > 1 else [text]
    words = _words(text)
    overlaps = [len(words & _words(s)) for s in symptoms]
    best = max(overlaps, default=0)
    symptom = overlaps.index(best) + 1 if best else 0
    context = next((code for code, pattern in _CONTEXTS if re.search(pattern, low)), "N")
    trigger = next((code for code, pattern in _TRIGGERS if re.search(pattern, low)), "x")
    long_time = "y" if re.search(_LONG_TIME, low) else "-"
    quote = 0
    if symptom and len(segs) > 1:
        target = _words(symptoms[symptom - 1])
        quote = next((i for i, seg in enumerate(segs) if _words(seg) & target), 0)
    return symptom, context, trigger, long_time, quote


_AREA = {"access": "access", "usability": "usability", "playback": "playback", "billing": "billing/support",
         "support": "billing/support"}


def _json_answer(request: ModelRequest) -> dict:
    pack = json.loads(request.user)
    if request.role == "group" and request.step == "name":
        return {"issues": [{"issue_id": i["issue_id"], "title": i["issue_id"].replace(".", " "),
                            "description": "Synthetic name from the fake provider.",
                            "example_review_ids": [e["review_id"] for e in i["examples"][:1]]} for i in pack["issues"]]}
    if request.role == "group" and request.step == "symptoms":
        ids = [e["review_id"] for e in pack["examples"]]
        return {"symptoms": [{"id": sid, "name": name, "definition": definition, "example_review_ids": ids[i:i + 1]}
                             for i, (sid, name, definition) in enumerate(_FAKE_SYMPTOMS[pack["area"]])]}
    if request.role == "group" and request.step == "discover":
        return {"topics": [{"topic": t["topic"], "subtopics": [
            {"id": "general", "name": f"General {t['topic']}", "definition": "Synthetic.", "example_review_ids": []}]}
            for t in pack["topics"]]}
    if request.step == "critic":
        return {"verdict": "pass", "problems": []}
    top = pack["facts"]["top_issues"][0]
    quotes = [{"issue_id": top["issue_id"], "quote": q["quote"][:40]} for q in top["quotes"][:1]]
    area = _AREA.get(top["issue_id"].split(".")[0], "usability")
    diagnosed = ((pack["facts"].get("diagnosis") or {}).get("areas") or {}).get(area) or {}
    fixable = [s for s in diagnosed.get("symptoms", []) if not s.get("plan_gated")][:2]
    fixes = [{"symptom_id": s["symptom_id"], "next_step": "Reproduce the reported behaviour and fix it."} for s in fixable]
    fix_first = "\nCode lists the symptoms below." if fixes else "\nNo diagnosis in the facts."
    return {"priority_area": area,
            "headline": "Synthetic memo from the fake provider.",
            "memo_markdown": (f"## Recommendation\nPrioritise `{top['issue_id']}`: {top['complaint_count']} complaints "
                              f"[C1] with mean severity {top['mean_severity']} [C2].\n## What to fix first{fix_first}\n"
                              "## Why\nSynthetic text.\n"
                              "## Free versus premium\nSynthetic text.\n## Alternatives\nSynthetic text.\n"
                              "## Limitations\nSynthetic text; not a finding."),
            "claims": [{"claim_id": "C1", "issue_id": top["issue_id"], "metric": "complaint_count",
                        "value": str(top["complaint_count"])},
                       {"claim_id": "C2", "issue_id": top["issue_id"], "metric": "mean_severity",
                        "value": top["mean_severity"]}],
            "fixes": fixes,
            "quotes": quotes}


class FakeVerifier:
    """Stand-in for Jev in offline tests: the same keyword rules, returned in Jev's shape."""
    name, model, tier, questions_version = "fake", "fake-rules-v0", "synthetic", "fake-q2"
    diagnosis_version = "fake-d1"

    def check_diagnosis(self, text: str, symptoms: list[dict]) -> dict:
        symptom, context, _, _, _ = diagnose_fields(text, [f"{s['name']}: {s['definition']}" for s in symptoms])
        record = {"symptom_id": symptoms[symptom - 1]["id"] if symptom else "other", "context": CONTEXT_CODES[context]}
        return {"ok": True, "record": record, "confidence": {"symptom": 0.9, "context": 0.9}, "probabilities": {},
                "request_id": f"fakev-{uuid.uuid4().hex}", "model": self.model,
                "input_tokens": len(text) // 4 + 400, "output_tokens": 0, "usage_available": False,
                "started_at": time.time(), "duration_ms": 0}

    def classify(self, text: str, segs: list[str], tax: dict) -> dict:
        _, topic, _, intent, severity, sentiment, _, _, limit, plan = _label(0, text, "fallback").split("|")
        record = {"topic": TOPIC_CODES[topic], "subtopic": tax["topics"][TOPIC_CODES[topic]][0]["id"],
                  "intent": INTENT_CODES[intent], "severity": int(severity), "sentiment": int(sentiment) / 10,
                  "entities": entities(text), "evidence_quote": segs[0], "needs_review": False,
                  "free_tier_limit": limit == "y", "plan": {"p": "paid", "f": "free", "u": "unstated"}[plan]}
        return {"ok": True, "record": record, "confidence": {"topic": 0.9, "intent": 0.9, "severity": 0.9},
                "probabilities": {}, "request_id": f"fakev-{uuid.uuid4().hex}", "model": self.model,
                "input_tokens": len(text) // 4 + 900, "output_tokens": 0, "usage_available": False,
                "started_at": time.time(), "duration_ms": 0}
