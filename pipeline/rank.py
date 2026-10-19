"""Stage 5 · code only: the baseline ranking, recomputed from saved files with no model call.

priority(issue) = complaint_count × mean_severity = severity_sum, kept as an integer so no
floating-point rounding is involved. Order: score descending, then issue id ascending.
"""
from __future__ import annotations

import csv
import gzip
import json
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

COLUMNS = ("rank", "issue_id", "complaint_count", "severity_sum", "mean_severity", "priority_score")


def read_jsonl(path: Path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def mean_string(total: int, n: int) -> str:
    """Six decimals, decimal half-up: the grader's rounding rule."""
    return str((Decimal(total) / Decimal(n)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def rank(records_path: Path, membership_path: Path) -> list[dict]:
    severity = {r["review_id"]: r["severity"] for r in read_jsonl(records_path)
                if r["status"] == "completed" and r["intent"] in ("complaint", "cancellation")}
    members: dict[str, list[int]] = defaultdict(list)
    seen = set()
    with Path(membership_path).open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            pair = (row["issue_id"], row["review_id"])
            if pair in seen:
                raise ValueError(f"Duplicate membership {pair}")
            if row["review_id"] not in severity:
                raise ValueError(f"Membership names a review that is not a completed complaint: {row['review_id']}")
            seen.add(pair)
            members[row["issue_id"]].append(severity[row["review_id"]])
    rows = [{"issue_id": issue, "complaint_count": len(values), "severity_sum": sum(values),
             "mean_severity": mean_string(sum(values), len(values)), "priority_score": sum(values)}
            for issue, values in members.items()]
    rows.sort(key=lambda r: (-r["priority_score"], r["issue_id"]))
    for position, row in enumerate(rows, 1):
        row["rank"] = position
    return rows


def write_ranking(rows: list[dict], out: Path) -> None:
    with Path(out).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows({c: row[c] for c in COLUMNS} for row in rows)
