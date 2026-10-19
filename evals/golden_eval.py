"""Golden-50 evaluation: the pipeline's enricher (and Jev, the challenger) against Gero's hand labels.

The models only ever see `data/golden_50_to_label.csv` (blank label columns). The human answers in
`evals/golden_50_labeled.csv` are read after the predictions exist, for scoring only.

    .venv/bin/python evals/golden_eval.py run         # paid, about a cent: predictions + scoring
    .venv/bin/python evals/golden_eval.py summarize   # offline: re-score saved predictions
"""
from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "evals" / "golden"
SOURCE = ROOT / "data" / "golden_50_to_label.csv"
LABELS = ROOT / "evals" / "golden_50_labeled.csv"                  # contract-corrected (main)
ORIGINAL = ROOT / "evals" / "golden_50_labeled.original.csv"       # Gero's labels before correction
RUN = "golden-haiku-s2"
SENTIMENT_TOLERANCE = 0.3  # declared before scoring


def predict_haiku() -> None:
    """The real pipeline, unchanged settings: prepare -> enrich -> fallback on the 50 golden texts."""
    py = str(ROOT / ".venv" / "bin" / "python")
    for step in (["prepare", "--input", str(SOURCE)], ["enrich", "--config", str(ROOT / "config" / "haiku.toml")],
                 ["fallback"]):
        subprocess.run([py, "-m", "pipeline", step[0], "--run", RUN, *step[1:]], cwd=ROOT, check=True,
                       capture_output=True, text=True)
    conn = sqlite3.connect(ROOT / "runs" / RUN / "pipeline.sqlite")
    rows = conn.execute("SELECT r.review_id, t.status, t.result, t.step FROM reviews r "
                        "LEFT JOIN texts t ON t.text_sha=r.text_sha").fetchall()
    with (OUT / "predictions_haiku.jsonl").open("w", encoding="utf-8") as f:
        for review_id, status, result, step in rows:
            f.write(json.dumps({"review_id": review_id, "status": status, "step": step,
                                **(json.loads(result) if status == "completed" else {})}, ensure_ascii=False) + "\n")
    usage = conn.execute("SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd) FROM calls").fetchone()
    (OUT / "usage_haiku.json").write_text(json.dumps(dict(zip(("calls", "input_tokens", "output_tokens", "cost_usd"),
                                                              usage)), indent=2) + "\n", encoding="utf-8")


def predict_jev() -> None:
    from pipeline.config import Settings, load_taxonomy
    from pipeline.providers.typesafe_jev import JevClassifier
    from pipeline.textproc import segments

    settings = Settings.load(ROOT / "config" / "haiku.toml")
    tax, jev = load_taxonomy(settings), JevClassifier()
    with SOURCE.open(encoding="utf-8-sig", newline="") as f, (OUT / "predictions_jev.jsonl").open("w", encoding="utf-8") as out:
        for row in csv.DictReader(f):
            result = jev.classify(row["review_text"], segments(row["review_text"], settings.quote_full_max), tax)
            out.write(json.dumps({"review_id": row["review_id"], "status": "completed" if result["ok"] else "failed",
                                  "input_tokens": result["input_tokens"], **(result.get("record") or {})},
                                 ensure_ascii=False) + "\n")


def score(predictions: dict, truth: list[dict]) -> dict:
    """Per-field agreement with the human labels. A missing prediction counts as wrong."""
    n = len(truth)
    hits, confusion, cases = Counter(), {"topic": Counter(), "intent": Counter()}, []
    severity_error, sentiment_error, sentiment_n = 0, 0.0, 0
    flagged = Counter(tp=0, fp=0, fn=0, tn=0)
    for t in truth:
        p = predictions.get(t["review_id"], {})
        ok = p.get("status") == "completed"
        accepted = {"topic": {t["topic"], t["alt_topic"]} - {""}, "intent": {t["intent"], t["alt_intent"]} - {""},
                    "severity": {int(t["severity"])} | ({int(t["alt_severity"])} if t["alt_severity"] else set())}
        row = {"review_id": t["review_id"], "text": t["review_text"][:160]}
        for field in ("topic", "intent", "severity"):
            good = ok and p[field] in accepted[field]
            hits[field] += good
            row[f"{field}_human"], row[f"{field}_model"] = t[field], p.get(field)
            row[f"{field}_ok"] = good
        hits["all_three"] += row["topic_ok"] and row["intent_ok"] and row["severity_ok"]
        for field, human in (("free_tier_limit", t.get("free_tier_limit", "")), ("plan", t.get("plan", ""))):
            if human:  # scored only once Gero has labelled the schema-s2 fields
                expected = (human == "true") if field == "free_tier_limit" else human
                hits[f"{field}_n"] += 1
                hits[f"{field}_ok"] += ok and p.get(field) == expected
                row[f"{field}_human"], row[f"{field}_model"] = human, p.get(field)
        if ok:
            severity_error += min(abs(p["severity"] - s) for s in accepted["severity"])
            sentiment_error += abs(p["sentiment"] - float(t["sentiment"]))
            sentiment_n += 1
            hits["sentiment_within_tolerance"] += abs(p["sentiment"] - float(t["sentiment"])) <= SENTIMENT_TOLERANCE
            hits["quote_is_substring"] += p["evidence_quote"] in t["review_text"]
            hits["quote_overlaps_human"] += p["evidence_quote"] in t["evidence_quote"] or t["evidence_quote"] in p["evidence_quote"]
            confusion["topic"][(t["topic"], p["topic"])] += 1
            confusion["intent"][(t["intent"], p["intent"])] += 1
            human_flag = t["needs_review"] == "true" or t["ambiguous"] == "true"
            model_flag = bool(p.get("needs_review"))
            flagged["tp" if human_flag and model_flag else "fp" if model_flag else "fn" if human_flag else "tn"] += 1
        cases.append(row)
    rate = lambda k: round(hits[k] / n, 3)
    return {"n": n, "valid_predictions": sentiment_n,
            "agreement": {k: rate(k) for k in ("topic", "intent", "severity", "all_three")},
            "severity_mae": round(severity_error / sentiment_n, 3) if sentiment_n else None,
            "sentiment_mae": round(sentiment_error / sentiment_n, 3) if sentiment_n else None,
            "sentiment_within_0.3": rate("sentiment_within_tolerance"),
            "quote_is_exact_substring": rate("quote_is_substring"), "quote_overlaps_human_quote": rate("quote_overlaps_human"),
            "segment_agreement": {f: {"n": hits[f"{f}_n"], "agreement": round(hits[f"{f}_ok"] / hits[f"{f}_n"], 3)
                                      if hits[f"{f}_n"] else None} for f in ("free_tier_limit", "plan")},
            "needs_review_vs_human_flag": dict(flagged),
            "confusion": {f: {f"{a} -> {b}": c for (a, b), c in sorted(m.items()) if a != b} for f, m in confusion.items()},
            "cases": cases}


def load(path: Path) -> dict:
    return {r["review_id"]: r for r in map(json.loads, path.open(encoding="utf-8"))} if path.exists() else {}


def _truth(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def summarize() -> str:
    truth = _truth(LABELS)
    results = {name: score(load(OUT / f"predictions_{name}.jsonl"), truth) for name in ("haiku", "jev")}
    original = {name: score(load(OUT / f"predictions_{name}.jsonl"), _truth(ORIGINAL))
                for name in ("haiku", "jev")} if ORIGINAL.exists() else None
    (OUT / "results.json").write_text(json.dumps({"contract_corrected": results, "original_labels": original},
                                                 indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with (OUT / "cases.csv").open("w", encoding="utf-8", newline="") as f:
        fields = ["review_id", "text"] + [f"{fl}_{k}" for fl in ("topic", "intent", "severity") for k in ("human", "model", "ok")]
        fields += [f"{fl}_{k}" for fl in ("free_tier_limit", "plan") for k in ("human", "model")]
        writer = csv.DictWriter(f, fieldnames=["system", *fields], lineterminator="\n", restval="")
        writer.writeheader()
        for name, r in results.items():
            writer.writerows({"system": name, **c} for c in r["cases"])
    lines = [f"Golden 50 (hand-labelled by Gero, then aligned with the contract; {sum(t['ambiguous'] == 'true' for t in truth)} "
             f"ambiguous cases, alternatives accepted). Sentiment tolerance declared before scoring: ±{SENTIMENT_TOLERANCE}.", "",
             "| Metric | Haiku 5.5 · contract labels | Jev · contract labels | Haiku 5.5 · original labels | Jev · original labels |",
             "|---|---:|---:|---:|---:|"]
    h, j = results["haiku"], results["jev"]
    rows = [("Topic agreement", "agreement", "topic"), ("Intent agreement", "agreement", "intent"),
            ("Severity exact agreement", "agreement", "severity"), ("All three right", "agreement", "all_three"),
            ("Severity MAE", "severity_mae", None), ("Sentiment MAE", "sentiment_mae", None),
            ("Sentiment within ±0.3", "sentiment_within_0.3", None),
            ("Quote is an exact substring", "quote_is_exact_substring", None),
            ("Quote overlaps the human quote", "quote_overlaps_human_quote", None)]
    for label, key, sub in rows:
        value = lambda r: r[key][sub] if sub else r[key]
        fmt = lambda v: "—" if v is None else f"{v:.0%}" if key not in ("severity_mae", "sentiment_mae") else f"{v:.2f}"
        extra = f" {fmt(value(original['haiku']))} | {fmt(value(original['jev']))} |" if original else " — | — |"
        lines.append(f"| {label} | {fmt(value(h))} | {fmt(value(j))} |" + extra)
    for field, label in (("free_tier_limit", "Free-tier limit agreement"), ("plan", "Stated plan agreement")):
        hs, js = h["segment_agreement"][field], j["segment_agreement"][field]
        if hs["n"]:
            lines.append(f"| {label} (n={hs['n']}) | {hs['agreement']:.0%} | {js['agreement']:.0%} | — | — |")
    lines += ["", f"Valid predictions: Haiku {h['valid_predictions']}/50, Jev {j['valid_predictions']}/50. "
              f"needs_review vs human flag (Haiku): {h['needs_review_vs_human_flag']}.", "",
              "Topic confusions (human -> model), Haiku: " + (", ".join(f"{k} ×{v}" for k, v in h["confusion"]["topic"].items()) or "none"),
              "Intent confusions (human -> model), Haiku: " + (", ".join(f"{k} ×{v}" for k, v in h["confusion"]["intent"].items()) or "none"),
              "Topic confusions, Jev: " + (", ".join(f"{k} ×{v}" for k, v in j["confusion"]["topic"].items()) or "none"),
              "Intent confusions, Jev: " + (", ".join(f"{k} ×{v}" for k, v in j["confusion"]["intent"].items()) or "none")]
    text = "\n".join(lines) + "\n"
    (OUT / "summary.md").write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        OUT.mkdir(parents=True, exist_ok=True)
        predict_haiku()
        predict_jev()
    print(summarize())
