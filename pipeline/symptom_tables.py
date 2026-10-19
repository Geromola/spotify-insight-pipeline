"""Code only: the diagnosis tables, from the saved records and diagnoses. No model call.

Counts are review rows (cache copies included: a copy inherits its origin's diagnosis by text).
diagnosis.json is the contract the memo and the dashboard read.
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from .config import DIAGNOSIS_AREAS
from .db import meta_get
from .diagnose import OTHER, bound_catalog, plan_gated_ids
from .rank import mean_string

VERSION_FLOOR = 2000  # an app version needs this many completed reviews before a share in it is reported
SYMPTOM_COLUMNS = ("area", "symptom_id", "symptom_name", "complaints", "severity_sum", "mean_severity",
                   "paying_complaints", "paying_severity_sum", "first6_complaints", "last6_complaints",
                   "last6_severity_sum", "likes_sum", "cancellations", "top_context", "top_context_share_pct",
                   "top_trigger", "top_trigger_share_pct", "top_version", "top_version_share_pct", "overall_share_pct",
                   "top_version_reviews", "plan_gated")
CONCRETE_KEYS = ("complaints", "severity_sum", "first6_complaints", "first6_severity_sum", "last6_complaints",
                 "last6_severity_sum", "paying_complaints", "paying_severity_sum", "other_complaints",
                 "other_last6_complaints", "plan_gated_complaints", "plan_gated_paying_complaints",
                 "plan_gated_last6_complaints")


def _pct(n: int, d: int, places: int) -> float | None:
    return round(100 * n / d, places) if d else None


def _likes(value) -> int:
    return int(value) if str(value or "").strip().isdigit() else 0


def _full_months(conn, run_dir: Path) -> list[str]:
    """Months with partial_month=0 in trends_month.csv: the memo facts' definition of a full month."""
    path = run_dir / "trends_month.csv"
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            return sorted({r["month"] for r in csv.DictReader(f) if r["partial_month"] == "0"})
    months = [r[0] for r in conn.execute("SELECT DISTINCT month FROM records ORDER BY 1")]
    return months[1:-1]


def _top(counter: Counter, total: int) -> tuple[str, float | None]:
    """The most frequent stated value ('none' only when nothing was stated), with its share of all rows."""
    stated = [(n, v) for v, n in counter.items() if v != "none"]
    if not stated:
        return ("none", _pct(counter.get("none", 0), total, 1)) if total else ("", None)
    n, value = min(stated, key=lambda x: (-x[0], x[1]))
    return value, _pct(n, total, 1)


def diagnosed_rows(conn) -> list[dict]:
    """Every diagnosable complaint row (copies included) whose text has a completed diagnosis."""
    topics = [t for ts in DIAGNOSIS_AREAS.values() for t in ts]
    return [dict(r) for r in conn.execute(f"""
        SELECT c.review_id, c.topic, c.intent, c.severity, c.plan, c.month, c.app_version, rv.review_likes AS likes,
               rv.text_sha, json_extract(t.result,'$.entities') AS entities, d.area, d.symptom_id, d.context,
               d.trigger, d.long_time_user, d.quote
        FROM records c JOIN reviews rv ON rv.review_id=c.review_id
        JOIN diagnoses d ON d.text_sha=rv.text_sha AND d.status='completed'
        JOIN texts t ON t.text_sha=rv.text_sha
        WHERE c.status='completed' AND c.intent IN ('complaint','cancellation') AND c.free_tier_limit=0
          AND c.topic IN ({','.join('?' * len(topics))})
        ORDER BY c.row_num""", topics)]


def run_plan_gated(conn) -> set[str]:
    """The plan-gated ids from the catalog file named in this run's saved settings (the default file otherwise)."""
    saved = json.loads(meta_get(conn, "settings", "{}") or "{}")
    return plan_gated_ids(saved.get("symptoms") or "symptoms.json")


def concrete_totals(rows: list[dict], gated: set[str], first6: set[str], last6: set[str]) -> dict:
    """Per area: concrete defects (a symptom that is neither "other" nor plan-gated), and what was set aside."""
    out = {area: dict.fromkeys(CONCRETE_KEYS, 0) for area in DIAGNOSIS_AREAS}
    for r in rows:
        t = out[r["area"]]
        if r["symptom_id"] == OTHER["id"]:
            t["other_complaints"] += 1
            t["other_last6_complaints"] += r["month"] in last6
        elif r["symptom_id"] in gated:
            t["plan_gated_complaints"] += 1
            t["plan_gated_paying_complaints"] += r["plan"] == "paid"
            t["plan_gated_last6_complaints"] += r["month"] in last6
        else:
            t["complaints"] += 1
            t["severity_sum"] += r["severity"]
            if r["month"] in first6:
                t["first6_complaints"] += 1
                t["first6_severity_sum"] += r["severity"]
            if r["month"] in last6:
                t["last6_complaints"] += 1
                t["last6_severity_sum"] += r["severity"]
            if r["plan"] == "paid":
                t["paying_complaints"] += 1
                t["paying_severity_sum"] += r["severity"]
    return out


def write_diagnosis(conn, run_dir: Path, version_floor: int = VERSION_FLOOR,
                    plan_gated: set[str] | None = None) -> dict | None:
    """Write diagnosis_symptoms.csv, diagnosis_context.csv, diagnosis_customers.csv and diagnosis.json."""
    catalog = bound_catalog(conn)
    has_records = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='records'").fetchone()[0]
    if catalog is None or not has_records:
        return None
    rows = diagnosed_rows(conn)
    gated = run_plan_gated(conn) if plan_gated is None else set(plan_gated)
    months = _full_months(conn, run_dir)
    first6, last6 = set(months[:6]), set(months[-6:])
    versions = {r[0]: r[1] for r in conn.execute(
        "SELECT app_version, COUNT(*) FROM records WHERE status='completed' AND trim(app_version)!='' "
        "GROUP BY app_version")}
    with_version = sum(versions.values())
    eligible = {v: n for v, n in versions.items() if n >= version_floor}

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["area"], r["symptom_id"])].append(r)
    table, contract = [], {}
    for area in DIAGNOSIS_AREAS:
        entries = []
        for symptom in [*catalog["areas"][area], OTHER]:
            members = groups.get((area, symptom["id"]), [])
            n = len(members)
            severity = sum(r["severity"] for r in members)
            paid = [r for r in members if r["plan"] == "paid"]
            late = [r for r in members if r["month"] in last6]
            context, context_share = _top(Counter(r["context"] for r in members), n)
            trigger, trigger_share = _top(Counter(r["trigger"] for r in members), n)
            in_version = Counter(r["app_version"] for r in members if r["app_version"].strip())
            shares = [(in_version[v] / d, in_version[v], v) for v, d in eligible.items() if in_version[v]]
            top_version = min(shares, key=lambda x: (-x[0], -x[1], x[2]))[2] if shares else ""
            row = {"area": area, "symptom_id": symptom["id"], "symptom_name": symptom["name"], "complaints": n,
                   "severity_sum": severity, "mean_severity": mean_string(severity, n) if n else "",
                   "paying_complaints": len(paid), "paying_severity_sum": sum(r["severity"] for r in paid),
                   "first6_complaints": sum(r["month"] in first6 for r in members), "last6_complaints": len(late),
                   "last6_severity_sum": sum(r["severity"] for r in late),
                   "likes_sum": sum(_likes(r["likes"]) for r in members),
                   "cancellations": sum(r["intent"] == "cancellation" for r in members),
                   "top_context": context, "top_context_share_pct": context_share,
                   "top_trigger": trigger, "top_trigger_share_pct": trigger_share,
                   "top_version": top_version,
                   "top_version_share_pct": _pct(in_version[top_version], eligible[top_version], 3) if top_version else None,
                   "overall_share_pct": _pct(sum(in_version.values()), with_version, 3) if n else None,
                   "top_version_reviews": eligible[top_version] if top_version else None,
                   "plan_gated": int(symptom["id"] in gated)}
            table.append(row)
            quotes, texts = [], set()
            for r in sorted(members, key=lambda r: (-_likes(r["likes"]), r["review_id"])):
                if r["text_sha"] in texts:
                    continue  # a copy repeats its origin's words
                texts.add(r["text_sha"])
                quotes.append({"review_id": r["review_id"], "quote": r["quote"], "likes": _likes(r["likes"])})
                if len(quotes) == 3:
                    break
            entries.append({"symptom_id": symptom["id"], "name": symptom["name"], "definition": symptom["definition"],
                            **{k: row[k] for k in ("complaints", "severity_sum", "last6_complaints",
                                                   "last6_severity_sum", "paying_complaints", "top_context",
                                                   "top_trigger", "top_version", "top_version_share_pct",
                                                   "overall_share_pct")},
                            "plan_gated": symptom["id"] in gated, "quotes": quotes})
        entries.sort(key=lambda e: (-e["severity_sum"], e["symptom_id"]))
        contract[area] = entries
    order = {area: i for i, area in enumerate(DIAGNOSIS_AREAS)}
    table.sort(key=lambda r: (order[r["area"]], -r["severity_sum"], r["symptom_id"]))
    _write_csv(run_dir / "diagnosis_symptoms.csv", SYMPTOM_COLUMNS,
               ([("" if r[c] is None else r[c]) for c in SYMPTOM_COLUMNS] for r in table))

    combos = defaultdict(lambda: [0, 0])
    for r in rows:
        combos[(r["area"], r["context"], r["trigger"])][0] += 1
        combos[(r["area"], r["context"], r["trigger"])][1] += r["severity"]
    _write_csv(run_dir / "diagnosis_context.csv", ("area", "context", "trigger", "complaints", "severity_sum"),
               ([a, c, t, n, s] for (a, c, t), (n, s) in sorted(combos.items(), key=lambda x: (order[x[0][0]], x[0]))))

    customers = []
    for area in DIAGNOSIS_AREAS:
        members = [r for r in rows if r["area"] == area]
        plans = Counter(r["plan"] for r in members)
        entity = lambda name: sum(name in json.loads(r["entities"] or "[]") for r in members)
        customers.append([area, len(members), sum(bool(r["long_time_user"]) for r in members), entity("family plan"),
                          entity("student plan"), plans.get("paid", 0), plans.get("free", 0), plans.get("unstated", 0)])
    _write_csv(run_dir / "diagnosis_customers.csv",
               ("area", "complaints", "long_time_users", "family_plan_mentions", "student_plan_mentions", "paid",
                "free", "unstated"), customers)

    counts = dict(conn.execute("SELECT status, COUNT(*) FROM diagnoses GROUP BY status").fetchall())
    completed = counts.get("completed", 0)
    other = conn.execute("SELECT COUNT(*) FROM diagnoses WHERE status='completed' AND symptom_id=?",
                         (OTHER["id"],)).fetchone()[0]
    result = {"catalog_version": catalog["version"],
              "coverage": {"diagnosable_texts": sum(counts.values()), "completed": completed,
                           "failed": counts.get("failed", 0), "other_share_pct": _pct(other, completed, 2)},
              "areas": contract, "concrete": concrete_totals(rows, gated, first6, last6)}
    (run_dir / "diagnosis.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def _write_csv(path: Path, header, rows) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
