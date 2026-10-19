"""Contract corrections to the golden 50, applied by Claude at Gero's request on 2026-10-17.

Gero labelled all 50 reviews by hand (`evals/golden_50_labeled.original.csv`, kept untouched). After the
first evaluation, Gero asked for his labels to be aligned with the grading contract's definitions. Only labels
that contradict a rule written in GRADING_CONTRACT.md / prompts/labels.md are changed. Where the rule still
leaves room for judgment, the contract reading becomes the main label and Gero's original stays as an accepted
alternative (ambiguous = true). Every change is listed in evals/golden/corrections.csv with its rule.

This happened after model predictions had been seen; the README discloses it and reports both scores.

    python3.12 evals/golden_corrections.py
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORIGINAL = ROOT / "evals" / "golden_50_labeled.original.csv"
CORRECTED = ROOT / "evals" / "golden_50_labeled.csv"
LOG = ROOT / "evals" / "golden" / "corrections.csv"

ADS = "Contract: ad interruptions are usability, not billing."
RECS = "Contract: recommendations and search/discovery are catalog."
BAD_APP = "Contract: general 'bad app' is a complaint; do not infer a specific defect (topic other)."
UNCLEAR_SEV = "Contract: severity 1 covers neutral or unclear content."
LANGUAGE = "Contract: label other languages by meaning when understood; original kept as an alternative."

# review_id prefix -> (new main labels, alternatives kept from Gero's labels, rule)
CHANGES = {
    "1fc8b08f": ({"topic": "usability", "intent": "praise", "sentiment": "0.8"}, {"alt_topic": "other", "alt_intent": "unclear"},
                 "Contract: for praise, the first specific praised feature (no ads -> usability); a paid-plan mention alone is not billing."),
    "47428620": ({"topic": "other"}, {}, BAD_APP),
    "01d453b0": ({"topic": "usability"}, {}, ADS),
    "ac6cd66d": ({"topic": "catalog", "intent": "praise", "sentiment": "0.6"}, {"alt_topic": "other", "alt_intent": "unclear"},
                 LANGUAGE + " Tagalog: 'all songs are on Spotify'."),
    "44129679": ({"severity": "1"}, {}, UNCLEAR_SEV),
    "46842184": ({"topic": "catalog", "intent": "complaint", "severity": "2"},
                 {"alt_topic": "other", "alt_intent": "unclear", "alt_severity": "1"},
                 LANGUAGE + " Indonesian: 'sometimes the lyrics are missing'."),
    "5de7f95b": ({"severity": "1"}, {}, UNCLEAR_SEV + " A bare boycott slogan is unclear."),
    "de48c99b": ({"topic": "catalog"}, {}, RECS),
    "372d4e67": ({"topic": "usability"}, {}, ADS),
    "8cc4fad4": ({"intent": "complaint"}, {"alt_intent": "unclear"},
                 "Contract: boycott text is unclear unless it states a product complaint; 'I hate this app' is a general complaint."),
    "46c0b49f": ({"severity": "3"}, {"alt_severity": "2"},
                 "Contract: severity 5 is only explicit serious financial, privacy or data harm; loud ads degrade listening."),
    "2aa566c6": ({"topic": "usability", "intent": "praise", "sentiment": "0.7"}, {"alt_intent": "unclear"},
                 ADS + " For praise, the first praised feature."),
    "8c0546b0": ({"topic": "catalog"}, {"alt_topic": "usability"},
                 "Contract: two problems of equal severity -> the first mentioned (lyrics availability is catalog)."),
    "b58dd6f0": ({"intent": "request"}, {"alt_intent": "praise"},
                 "Contract: intent precedence puts request (a desired change without a failure) before praise."),
    "5fba12ea": ({"topic": "catalog"}, {}, RECS + " First praised feature: finding any music."),
    "ac4e860a": ({"topic": "downloads"}, {"alt_topic": "playback"},
                 "Contract: offline listening is downloads; paying for the plan does not make it billing."),
    "9034d675": ({"topic": "usability"}, {}, ADS),
    "d2f3874f": ({"topic": "usability"}, {"alt_topic": "billing"},
                 "Contract: the most severe problem wins (free tier unusable with constant ads > price too high)."),
    "99b93e31": ({"topic": "other"}, {"alt_topic": "usability"}, BAD_APP + " 'This update very bad' names no defect."),
}


def main() -> None:
    with ORIGINAL.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    by_prefix = {r["review_id"][:8]: r for r in rows}
    assert set(CHANGES) <= set(by_prefix), set(CHANGES) - set(by_prefix)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log = []
    for prefix, (main, alts, rule) in CHANGES.items():
        row = by_prefix[prefix]
        before = {k: row[k] for k in ("topic", "intent", "severity")}
        row.update(main)
        if alts:
            row.update(alts)
            row["ambiguous"] = "true"
        note = f"Contract correction by Claude at Gero's request on {stamp}, after seeing model predictions: {rule} " \
               f"Original: {before['topic']}/{before['intent']}/{before['severity']}."
        row["notes"] = f"{row['notes']} | {note}" if row["notes"] else note
        log.append({"review_id": row["review_id"], "text": " ".join(row["review_text"].split())[:100],
                    "original": f"{before['topic']}/{before['intent']}/{before['severity']}",
                    "corrected": f"{row['topic']}/{row['intent']}/{row['severity']}",
                    "alternatives_kept": " ".join(f"{k}={v}" for k, v in alts.items()), "rule": rule})
    with CORRECTED.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(log[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(log)
    print(f"{len(log)} of {len(rows)} reviews corrected; {sum(bool(l['alternatives_kept']) for l in log)} keep "
          f"Gero's label as an accepted alternative. Log: {LOG.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
