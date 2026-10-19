"""Stage 3 · the verifier role: a different model re-labels a declared random sample, blind.

The verifier sees only the review text. It never sees the enricher's labels; code does the
comparison afterwards and writes the agreement report and the list of disagreements.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import time
from collections import Counter
from pathlib import Path

from .budget import BudgetStop, Rates, admit
from .config import Settings, load_taxonomy
from .db import tx
from .providers import ModelResponse
from .roles import record_call
from .textproc import estimate_tokens, segments

FIELDS = ("topic", "intent", "severity")
SEGMENT_FIELDS = ("free_tier_limit", "plan")  # agreement reported separately; disagreements list stays on FIELDS
TOPIC_ORDER = ("access", "usability", "playback", "downloads", "catalog", "billing", "support", "other")


def get_verifier(settings: Settings):
    if settings.verifier == "jev":
        from .providers.typesafe_jev import JevClassifier
        return JevClassifier()
    if settings.verifier == "fake":
        from .providers.fake import FakeVerifier
        return FakeVerifier()
    raise ValueError(f"Unknown verifier '{settings.verifier}'")


def declare_sample(conn, settings: Settings, run_dir: Path) -> list:
    """Lowest SHA-256(seed:review_id) among completed unique texts, written down before any call."""
    rows = conn.execute("SELECT text_sha, text, origin_review_id FROM texts WHERE status='completed'").fetchall()
    n = min(settings.verify_max, math.ceil(len(rows) * settings.verify_fraction)) if rows else 0
    rank = lambda r: hashlib.sha256(f"{settings.verify_seed}:{r['origin_review_id']}".encode()).hexdigest()
    sample = sorted(rows, key=rank)[:n]
    (run_dir / "verify_sample.json").write_text(json.dumps({
        "method": "lowest SHA-256(seed + ':' + review_id) among completed unique texts",
        "seed": settings.verify_seed, "fraction": settings.verify_fraction, "max": settings.verify_max,
        "population": len(rows), "n": len(sample), "review_ids": [r["origin_review_id"] for r in sample]},
        indent=2) + "\n", encoding="utf-8")
    return sample


def run_verify(conn, settings: Settings, run_dir: Path, invocation: int, verifier=None, rates: Rates | None = None,
               sleep=time.sleep) -> dict:
    verifier = verifier or get_verifier(settings)
    rates = rates or Rates()
    tax = load_taxonomy(settings)
    label_config = f"{verifier.name}:{verifier.model}|{verifier.questions_version}"
    done = {r[0] for r in conn.execute("SELECT text_sha FROM verifications")}
    stopped, calls = None, 0
    for row in declare_sample(conn, settings, run_dir):
        if row["text_sha"] in done:
            continue
        reservation = rates.cost(verifier.name, verifier.model, verifier.tier,
                                 estimate_tokens(len(row["text"])) + 1200, 0, 0)
        try:
            admit(conn, settings, "verify", reservation)
        except BudgetStop as stop:
            stopped = f"budget: {stop}"
            break
        failures = 0
        while True:
            out = verifier.classify(row["text"], segments(row["text"], settings.quote_full_max), tax)
            calls += 1
            resp = ModelResponse(request_id=out["request_id"], ok=out["ok"], error=out.get("error"),
                                 text=json.dumps(out.get("record")) if out["ok"] else "",
                                 input_tokens=out["input_tokens"], output_tokens=out["output_tokens"],
                                 usage_available=out["usage_available"], started_at=out["started_at"],
                                 duration_ms=out["duration_ms"])
            record_call(conn, role="verify", step="verify", provider=verifier.name, model=verifier.model,
                        effort=None, tier=verifier.tier, label_config=label_config, invocation=invocation,
                        resp=resp, review_ids=[row["origin_review_id"]],
                        cost=rates.cost(verifier.name, verifier.model, verifier.tier, resp.input_tokens, 0,
                                        resp.output_tokens))
            if out["ok"]:
                record = out["record"]
                with tx(conn):
                    conn.execute("INSERT OR REPLACE INTO verifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (row["text_sha"], row["origin_review_id"], out["request_id"], record["topic"],
                                  record["intent"], record["severity"], record["sentiment"],
                                  json.dumps(out["confidence"]), json.dumps(out["probabilities"]), time.time(),
                                  int(record["free_tier_limit"]), record["plan"]))
                break
            failures += 1
            if out.get("fatal") or failures > settings.transport_retries:
                stopped = f"verifier failed: {out.get('error')}"
                break
            backoff = min(60.0, settings.backoff_seconds * 2 ** (failures - 1)) * (0.5 + random.random())
            sleep(max(backoff, out.get("retry_after") or 0.0))
        if stopped:
            break
    report = compare(conn, run_dir)
    report.update({"calls_this_run": calls, "stopped": stopped, "verifier": label_config})
    (run_dir / "verify_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    planted_error_test(conn, run_dir)
    return report


def _pairs(conn) -> list[tuple[dict, dict, str, str]]:
    rows = conn.execute("SELECT v.*, t.result, t.text FROM verifications v JOIN texts t USING(text_sha) "
                        "WHERE t.status='completed' ORDER BY v.origin_review_id").fetchall()
    pairs = []
    for r in rows:
        verified = {k: r[k] for k in (*FIELDS, "sentiment", "confidence", "plan")}
        verified["free_tier_limit"] = bool(r["free_tier_limit"]) if r["free_tier_limit"] is not None else None
        pairs.append((json.loads(r["result"]), verified, r["origin_review_id"], r["text"]))
    return pairs


def agreement(pairs: list[tuple[dict, dict, str, str]]) -> dict:
    """Pure comparison: per-field agreement, severity error, topic confusion, disagreements."""
    n = len(pairs)
    agree = Counter()
    confusion = Counter()
    disagreements = []
    abs_error = 0
    for enriched, verified, review_id, text in pairs:
        differing = [f for f in FIELDS if enriched[f] != verified[f]]
        for f in FIELDS:
            agree[f] += f not in differing
        abs_error += abs(enriched["severity"] - verified["severity"])
        confusion[(enriched["topic"], verified["topic"])] += 1
        if differing:
            disagreements.append({"review_id": review_id, "fields": differing,
                                  "enricher": {f: enriched[f] for f in FIELDS},
                                  "verifier": {f: verified[f] for f in FIELDS},
                                  "verifier_confidence": verified.get("confidence"), "text": text[:300]})
    segment = {}
    for f in SEGMENT_FIELDS:  # only where both sides carry the field (schema s2)
        both = [(e.get(f), v.get(f)) for e, v, _, _ in pairs if e.get(f) is not None and v.get(f) is not None]
        segment[f] = {"n": len(both), "agreement": round(sum(a == b for a, b in both) / len(both), 4) if both else None,
                      "enricher_true_or_paid": sum(a in (True, "paid") for a, _ in both),
                      "verifier_true_or_paid": sum(b in (True, "paid") for _, b in both)}
    return {"n": n, "segment_agreement": segment, "agreement": {f: round(agree[f] / n, 4) if n else None for f in FIELDS},
            "all_three_agree": round((n - len(disagreements)) / n, 4) if n else None,
            "severity_mae": round(abs_error / n, 4) if n else None,
            "topic_confusion": {f"{a}->{b}": c for (a, b), c in sorted(confusion.items())},
            "disagreements": disagreements}


def compare(conn, run_dir: Path) -> dict:
    report = agreement(_pairs(conn))
    with (run_dir / "verify_disagreements.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["review_id", "fields", "enricher_topic", "verifier_topic", "enricher_intent",
                         "verifier_intent", "enricher_severity", "verifier_severity", "text"])
        for d in report["disagreements"]:
            e, v = d["enricher"], d["verifier"]
            writer.writerow([d["review_id"], "+".join(d["fields"]), e["topic"], v["topic"], e["intent"], v["intent"],
                             e["severity"], v["severity"], d["text"]])
    return report


def planted_error_test(conn, run_dir: Path, k: int = 20) -> dict:
    """A separate test copy with deliberately wrong enricher labels: the comparison must flag them.

    No model call; the copy never touches the real records or any business aggregate.
    """
    clean = [p for p in _pairs(conn) if all(p[0][f] == p[1][f] for f in FIELDS)][:k]
    planted = []
    for i, (enriched, verified, review_id, text) in enumerate(clean):
        wrong = dict(enriched)
        field = "topic" if i % 2 == 0 else "severity"
        if field == "topic":
            wrong["topic"] = TOPIC_ORDER[(TOPIC_ORDER.index(enriched["topic"]) + 1) % len(TOPIC_ORDER)]
        else:
            wrong["severity"] = enriched["severity"] % 5 + 1
        planted.append(((wrong, verified, review_id, text), field))
    flagged = agreement([p for p, _ in planted])["disagreements"]
    caught = {d["review_id"]: d["fields"] for d in flagged}
    detected = sum(field in caught.get(p[2], []) for p, field in planted)
    result = {"synthetic": True, "planted": len(planted), "detected": detected,
              "rate": round(detected / len(planted), 4) if planted else None,
              "note": "Wrong labels planted in a copy of verified-agreeing records; no model call, "
                      "excluded from every business aggregate."}
    (run_dir / "verify_planted.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


# ── the blind check of the diagnose stage ──────────────────────────────────────
def declare_diagnosis_sample(conn, settings: Settings, run_dir: Path) -> list:
    """Lowest SHA-256(seed:review_id) among completed diagnoses, written down before any call."""
    rows = conn.execute("SELECT d.text_sha, d.area, d.symptom_id, d.context, t.text, t.origin_review_id "
                        "FROM diagnoses d JOIN texts t ON t.text_sha=d.text_sha WHERE d.status='completed'").fetchall()
    rank = lambda r: hashlib.sha256(f"{settings.diagnose_verify_seed}:{r['origin_review_id']}".encode()).hexdigest()
    sample = sorted(rows, key=rank)[:settings.diagnose_verify_n]
    (run_dir / "diagnosis_verify_sample.json").write_text(json.dumps({
        "method": "lowest SHA-256(seed + ':' + review_id) among completed diagnoses",
        "seed": settings.diagnose_verify_seed, "max": settings.diagnose_verify_n, "population": len(rows),
        "n": len(sample), "review_ids": [r["origin_review_id"] for r in sample]}, indent=2) + "\n", encoding="utf-8")
    return sample


def run_diagnosis_check(conn, settings: Settings, run_dir: Path, invocation: int, catalog: dict, verifier=None,
                        rates: Rates | None = None, sleep=time.sleep) -> dict:
    """One blind verifier call per sampled text: which of the area's symptoms (+ other), which context.

    Spend draws on the diagnose cap. Code compares afterwards and writes diagnosis_verify.json.
    """
    verifier = verifier or get_verifier(settings)
    rates = rates or Rates()
    label_config = f"{verifier.name}:{verifier.model}|{verifier.diagnosis_version}"
    done = {r[0] for r in conn.execute("SELECT text_sha FROM diagnosis_checks")}
    stopped, calls = None, 0
    sample = declare_diagnosis_sample(conn, settings, run_dir)
    for row in sample:
        if row["text_sha"] in done:
            continue
        reservation = rates.cost(verifier.name, verifier.model, verifier.tier,
                                 estimate_tokens(len(row["text"])) + 1200, 0, 0)
        try:
            admit(conn, settings, "diagnose", reservation)
        except BudgetStop as stop:
            stopped = f"budget: {stop}"
            break
        failures = 0
        while True:
            out = verifier.check_diagnosis(row["text"], catalog["areas"][row["area"]])
            calls += 1
            resp = ModelResponse(request_id=out["request_id"], ok=out["ok"], error=out.get("error"),
                                 text=json.dumps(out.get("record")) if out["ok"] else "",
                                 input_tokens=out["input_tokens"], output_tokens=out["output_tokens"],
                                 usage_available=out["usage_available"], started_at=out["started_at"],
                                 duration_ms=out["duration_ms"])
            record_call(conn, role="verify", step="diagnose", provider=verifier.name, model=verifier.model,
                        effort=None, tier=verifier.tier, label_config=label_config, invocation=invocation,
                        resp=resp, review_ids=[row["origin_review_id"]],
                        cost=rates.cost(verifier.name, verifier.model, verifier.tier, resp.input_tokens, 0,
                                        resp.output_tokens))
            if out["ok"]:
                with tx(conn):
                    conn.execute("INSERT OR REPLACE INTO diagnosis_checks VALUES(?,?,?,?,?,?,?)",
                                 (row["text_sha"], row["origin_review_id"], out["request_id"],
                                  out["record"]["symptom_id"], out["record"]["context"],
                                  json.dumps(out["confidence"]), time.time()))
                break
            failures += 1
            if out.get("fatal") or failures > settings.transport_retries:
                stopped = f"verifier failed: {out.get('error')}"
                break
            backoff = min(60.0, settings.backoff_seconds * 2 ** (failures - 1)) * (0.5 + random.random())
            sleep(max(backoff, out.get("retry_after") or 0.0))
        if stopped:
            break
    sampled = {r["text_sha"] for r in sample}
    pairs = [r for r in conn.execute(
        "SELECT d.text_sha, d.area, d.symptom_id, d.context, c.symptom_id AS v_symptom, c.context AS v_context, "
        "c.confidence, c.origin_review_id, t.text FROM diagnosis_checks c JOIN diagnoses d ON d.text_sha=c.text_sha "
        "JOIN texts t ON t.text_sha=c.text_sha WHERE d.status='completed' ORDER BY c.origin_review_id")
             if r["text_sha"] in sampled]
    report = diagnosis_agreement(pairs)
    report.update({"method": "lowest SHA-256(seed + ':' + review_id) among completed diagnoses; one blind call per "
                             "text: the area's symptom names (+ other) and the context meanings",
                   "seed": settings.diagnose_verify_seed, "sample": len(sample), "calls_this_run": calls,
                   "stopped": stopped, "verifier": label_config})
    (run_dir / "diagnosis_verify.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                                   encoding="utf-8")
    return report


def diagnosis_agreement(pairs: list, listed: int = 50) -> dict:
    """Pure comparison of the diagnoser and the verifier on symptom and context, overall and per area."""
    n = len(pairs)
    agree, by_area, disagreements = Counter(), {}, []
    for r in pairs:
        same = {"symptom": r["symptom_id"] == r["v_symptom"], "context": r["context"] == r["v_context"]}
        agree.update(f for f, ok in same.items() if ok)
        area = by_area.setdefault(r["area"], Counter())
        area["n"] += 1
        area.update(f for f, ok in same.items() if ok)
        if not all(same.values()):
            disagreements.append({"review_id": r["origin_review_id"], "area": r["area"],
                                  "fields": [f for f, ok in same.items() if not ok],
                                  "diagnoser": {"symptom_id": r["symptom_id"], "context": r["context"]},
                                  "verifier": {"symptom_id": r["v_symptom"], "context": r["v_context"]},
                                  "verifier_confidence": json.loads(r["confidence"] or "null"),
                                  "text": r["text"][:300]})
    share = lambda k, total: round(k / total, 4) if total else None
    return {"n": n, "agreement": {"symptom": share(agree["symptom"], n), "context": share(agree["context"], n)},
            "by_area": {a: {"n": c["n"], "symptom": share(c["symptom"], c["n"]), "context": share(c["context"], c["n"])}
                        for a, c in sorted(by_area.items())},
            "disagreements_total": len(disagreements), "disagreements": disagreements[:listed]}
