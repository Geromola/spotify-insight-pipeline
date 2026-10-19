"""Build the dashboard database bundle from a finalized pipeline run.

Standard library only. Reads a run directory (read-only) plus the repo-level evaluation and cost files,
and writes `schema.sql` and one CSV per table into `--out`:

    python3.12 dashboard/db/build_bundle.py --run runs/pilot-100-t2 --out dashboard/db/bundle

The CSVs are in PostgreSQL `COPY ... (FORMAT csv, HEADER true)` form: every non-null value is quoted and a
NULL is an unquoted empty field, so an empty string and a NULL stay distinct. The reviews table is
streamed row by row from SQLite, so a 660,622-row run never sits in memory.

`schema.sql` drops and recreates every dashboard table. The line `-- @indexes` splits it: loaders run the
part above it, COPY the CSVs, then run the part below it, so indexes are built once after the data lands.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INDEX_MARKER = "-- @indexes"
COMPLAINT_INTENTS = ("complaint", "cancellation")

# One definition drives both the DDL and the CSV header, so the two cannot drift apart.
TABLES: dict[str, list[tuple[str, str]]] = {
    "reviews": [
        ("review_id", "text PRIMARY KEY"), ("review_text", "text"), ("rating", "smallint"),
        ("likes", "integer"), ("app_version", "text"), ("review_ts", "timestamp"), ("month", "text"),
        ("status", "text"), ("reason", "text"), ("topic", "text"), ("subtopic", "text"),
        ("issue_id", "text"), ("intent", "text"), ("severity", "smallint"), ("sentiment", "real"),
        ("needs_review", "boolean"), ("free_tier_limit", "boolean"), ("plan", "text"),
        ("evidence_quote", "text"), ("cache_source_id", "text"),
    ],
    "issues": [
        ("issue_id", "text PRIMARY KEY"), ("topic", "text"), ("subtopic", "text"), ("name", "text"),
        ("definition", "text"), ("title", "text"), ("description", "text"), ("example_review_ids", "jsonb"),
        ("rank", "integer"), ("complaint_count", "integer"), ("severity_sum", "integer"),
        ("mean_severity", "double precision"), ("priority_score", "double precision"),
        ("cancellations", "integer"),
    ],
    "trends_month": [
        ("month", "text"), ("issue_id", "text"), ("complaints", "integer"), ("completed_in_month", "integer"),
        ("share", "double precision"), ("partial_month", "boolean"),
    ],
    "trends_version": [
        ("app_version", "text"), ("issue_id", "text"), ("complaints", "integer"),
        ("completed_in_version", "integer"), ("share", "double precision"),
    ],
    "segment_ranking": [
        ("lens", "text"), ("rank", "integer"), ("issue_id", "text"), ("complaint_count", "integer"),
        ("severity_sum", "integer"), ("mean_severity", "double precision"), ("priority_score", "double precision"),
    ],
    "segment_areas": [
        ("area", "text"), ("free_tier_limit", "boolean"), ("plan", "text"), ("complaints", "integer"),
        ("severity_sum", "integer"),
    ],
    "areas": [
        ("area", "text PRIMARY KEY"), ("position", "smallint"), ("topics", "jsonb"), ("complaints", "integer"),
        ("severity_sum", "integer"), ("share_pct", "double precision"), ("in_question", "boolean"),
    ],
    "memo": [
        ("id", "smallint PRIMARY KEY"), ("question", "text"), ("ranking_rule", "text"), ("priority_area", "text"),
        ("headline", "text"), ("memo_markdown", "text"), ("quotes", "jsonb"), ("revised", "boolean"),
        ("critique", "jsonb"), ("final_critique", "jsonb"), ("final_problems", "jsonb"),
        ("draft_problems", "jsonb"),
    ],
    "claims": [
        ("claim_id", "text PRIMARY KEY"), ("position", "smallint"), ("issue_id", "text"), ("metric", "text"),
        ("value", "text"),
    ],
    "stages": [
        ("role", "text"), ("step", "text"), ("provider", "text"), ("model", "text"), ("effort", "text"),
        ("tier", "text"), ("calls", "integer"), ("failed", "integer"), ("input_tokens", "bigint"),
        ("cached_input_tokens", "bigint"), ("cache_write_tokens", "bigint"), ("output_tokens", "bigint"),
        ("reasoning_tokens", "bigint"), ("cost_usd", "double precision"), ("duration_ms", "bigint"),
        ("first_started_at", "timestamptz"), ("last_started_at", "timestamptz"),
    ],
    # Diagnosis stage: one approved-catalog symptom per in-scope complaint, plus stated context and trigger.
    "diagnosis_symptoms": [
        ("area", "text"), ("symptom_id", "text"), ("name", "text"), ("definition", "text"),
        ("complaints", "integer"), ("severity_sum", "integer"), ("mean_severity", "double precision"),
        ("paying_complaints", "integer"), ("paying_severity_sum", "integer"), ("first6_complaints", "integer"),
        ("last6_complaints", "integer"), ("last6_severity_sum", "integer"), ("likes_sum", "integer"),
        ("cancellations", "integer"), ("top_context", "text"), ("top_context_share_pct", "double precision"),
        ("top_trigger", "text"), ("top_trigger_share_pct", "double precision"), ("top_version", "text"),
        ("top_version_share_pct", "double precision"), ("overall_share_pct", "double precision"),
        ("top_version_reviews", "integer"), ("plan_gated", "boolean"),
    ],
    "diagnosis_context": [
        ("area", "text"), ("context", "text"), ("trigger", "text"), ("complaints", "integer"),
        ("severity_sum", "integer"),
    ],
    "diagnosis_customers": [
        ("area", "text PRIMARY KEY"), ("complaints", "integer"), ("long_time_users", "integer"),
        ("family_plan_mentions", "integer"), ("student_plan_mentions", "integer"), ("paid", "integer"),
        ("free", "integer"), ("unstated", "integer"),
    ],
    # Quote text and likes only: the review a quote came from never leaves the run directory.
    "diagnosis_quotes": [
        ("area", "text"), ("symptom_id", "text"), ("rank", "smallint"), ("quote", "text"), ("likes", "integer"),
    ],
    "kv": [("key", "text PRIMARY KEY"), ("value", "jsonb")],
}

EXTRA_CONSTRAINTS = {
    "trends_month": "PRIMARY KEY (month, issue_id)",
    "trends_version": "PRIMARY KEY (app_version, issue_id)",
    "stages": "PRIMARY KEY (role, step)",
    "segment_ranking": "PRIMARY KEY (lens, issue_id)",
    "segment_areas": "PRIMARY KEY (area, free_tier_limit, plan)",
    # The only diagnosis key: the tables are tiny and Neon is near its storage limit, so no other indexes.
    "diagnosis_symptoms": "PRIMARY KEY (area, symptom_id)",
}

# ranking_segments.csv names its lenses in words; the database stores a short key the API allow-lists.
LENS_KEYS = {
    "all complaints (baseline)": "baseline",
    "without free-tier limits": "no_free_limits",
    "paying reviewers only": "paying",
}
SEGMENT_FILES = ("ranking_segments.csv", "segments_area.csv")
SEGMENT_COLUMNS = ("free_tier_limit", "plan")

# The evidence query and the paged review list both order by this key; the indexes match it exactly.
INDEXES = [
    "CREATE INDEX reviews_issue_likes ON reviews (issue_id, likes DESC NULLS LAST, severity DESC NULLS LAST, review_id)",
    # Only the indexes the API's hot paths use: the free Neon tier stores 0.5 GB, and 660k reviews with text
    # already take most of it. Unfiltered review listings and status counts are cached at the edge instead.
    "CREATE INDEX trends_month_issue ON trends_month (issue_id, month)",
]


def schema_sql(run_id: str) -> str:
    lines = [f"-- Generated by dashboard/db/build_bundle.py from run {run_id}.",
             "-- Drops and recreates every dashboard table; loaders COPY the CSVs between the two halves.", ""]
    for name, cols in TABLES.items():
        body = [f"  {col} {typ}" for col, typ in cols]
        if name in EXTRA_CONSTRAINTS:
            body.append(f"  {EXTRA_CONSTRAINTS[name]}")
        lines.append(f"DROP TABLE IF EXISTS {name};")
        lines.append(f"CREATE TABLE {name} (\n" + ",\n".join(body) + "\n);")
        lines.append("")
    lines.append(INDEX_MARKER)
    lines.extend(f"{stmt};" for stmt in INDEXES)
    lines.append("ANALYZE;")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- value cleaning

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")


def text(v):
    if v is None:
        return None
    s = str(v).replace("\x00", "")
    return s


def integer(v, lo=-2_147_483_648, hi=2_147_483_647):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f != int(f) or not lo <= f <= hi:
        return None
    return int(f)


def small(v):
    return integer(v, -32768, 32767)


def number(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def boolean(v):
    if v is None or v == "":
        return None
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("1", "t", "true", "yes"):
            return "t"
        if low in ("0", "f", "false", "no"):
            return "f"
        return None
    return "t" if v else "f"


def timestamp(v):
    s = text(v)
    return s.strip() if s and TS_RE.match(s.strip()) else None


def epoch_iso(v):
    f = number(v)
    return datetime.fromtimestamp(f, timezone.utc).isoformat() if f is not None else None


def clean_json(o):
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {str(k): clean_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean_json(v) for v in o]
    if isinstance(o, str):
        return o.replace("\x00", "")
    return o


def jsonb(o):
    return None if o is None else json.dumps(clean_json(o), ensure_ascii=False, allow_nan=False)


# ---------------------------------------------------------------- io helpers


class TableWriter:
    def __init__(self, out: Path, name: str):
        self.name = name
        self.cols = [c for c, _ in TABLES[name]]
        self.fh = open(out / f"{name}.csv", "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.fh, quoting=csv.QUOTE_NOTNULL, lineterminator="\n")
        self.w.writerow(self.cols)
        self.rows = 0

    def write(self, row: dict):
        self.w.writerow([row.get(c) for c in self.cols])
        self.rows += 1

    def close(self) -> int:
        self.fh.close()
        return self.rows


def warn(msg: str):
    print(f"warning: {msg}", file=sys.stderr)


def read_json(path: Path):
    if not path.exists():
        warn(f"missing {path}")
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        warn(f"missing {path}")
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def stream_csv(path: Path):
    if not path.exists():
        warn(f"missing {path}")
        return
    with open(path, newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


# ---------------------------------------------------------------- tables


def build_reviews(con: sqlite3.Connection, out: Path) -> tuple[int, Counter, Counter]:
    """Stream one row per source review. Returns the row count and per-issue complaint/cancellation counts."""
    complaints: Counter = Counter()
    cancellations: Counter = Counter()
    w = TableWriter(out, "reviews")
    cur = con.execute(
        """
        SELECT r.review_id, t.text, r.review_rating, v.review_likes, r.app_version, v.review_timestamp,
               r.month, r.status, r.reason, r.topic, r.subtopic, r.intent, r.severity, r.sentiment,
               r.needs_review, r.free_tier_limit, r.plan,
               CASE WHEN json_valid(t.result) THEN json_extract(t.result, '$.evidence_quote') END,
               r.cache_source_id, v.app_version
        FROM records r
        LEFT JOIN reviews v ON v.review_id = r.review_id
        LEFT JOIN texts t ON t.text_sha = v.text_sha
        """
    )
    for (review_id, review_text, rating, likes, app_version, ts, month, status, reason, topic, subtopic,
         intent, severity, sentiment, needs_review, free_tier_limit, plan, quote, cache_source_id,
         src_version) in cur:
        issue_id = None
        if intent in COMPLAINT_INTENTS and topic and subtopic:
            issue_id = f"{topic}.{subtopic}"
            complaints[issue_id] += 1
            if intent == "cancellation":
                cancellations[issue_id] += 1
        w.write({
            "review_id": text(review_id), "review_text": text(review_text), "rating": small(rating),
            "likes": integer(likes), "app_version": text(app_version or src_version or None),
            "review_ts": timestamp(ts), "month": text(month), "status": text(status), "reason": text(reason or None),
            "topic": text(topic), "subtopic": text(subtopic), "issue_id": issue_id, "intent": text(intent),
            "severity": small(severity), "sentiment": number(sentiment), "needs_review": boolean(needs_review),
            "free_tier_limit": boolean(free_tier_limit), "plan": text(plan or None),
            "evidence_quote": text(quote), "cache_source_id": text(cache_source_id or None),
        })
    return w.close(), complaints, cancellations


def build_issues(run: Path, out: Path, complaints: Counter, cancellations: Counter) -> int:
    ranking = {r["issue_id"]: r for r in read_csv(run / "ranking.csv")}
    named = {r["issue_id"]: r for r in read_csv(run / "issues.csv")}
    w = TableWriter(out, "issues")
    for issue_id in sorted(set(ranking) | set(named)):
        rk, nm = ranking.get(issue_id, {}), named.get(issue_id, {})
        topic, _, subtopic = issue_id.partition(".")
        if rk and integer(rk.get("complaint_count")) != complaints.get(issue_id, 0):
            warn(f"{issue_id}: ranking.csv says {rk.get('complaint_count')} complaints, "
                 f"records say {complaints.get(issue_id, 0)}")
        examples = [x for x in (nm.get("example_review_ids") or "").replace(";", " ").replace(",", " ").split() if x]
        w.write({
            "issue_id": issue_id, "topic": nm.get("topic") or topic, "subtopic": nm.get("subtopic") or subtopic,
            "name": nm.get("name") or None, "definition": nm.get("definition") or None,
            "title": nm.get("title") or None, "description": nm.get("description") or None,
            "example_review_ids": jsonb(examples), "rank": integer(rk.get("rank")),
            "complaint_count": integer(rk.get("complaint_count")) if rk else 0,
            "severity_sum": integer(rk.get("severity_sum")) if rk else 0,
            "mean_severity": number(rk.get("mean_severity")), "priority_score": number(rk.get("priority_score")),
            "cancellations": cancellations.get(issue_id, 0),
        })
    return w.close()


def build_trends(run: Path, out: Path) -> tuple[int, int]:
    w = TableWriter(out, "trends_month")
    for r in stream_csv(run / "trends_month.csv"):
        w.write({"month": r.get("month"), "issue_id": r.get("issue_id"), "complaints": integer(r.get("complaints")),
                 "completed_in_month": integer(r.get("completed_in_month")), "share": number(r.get("share")),
                 "partial_month": boolean(r.get("partial_month"))})
    n_month = w.close()
    w = TableWriter(out, "trends_version")
    for r in stream_csv(run / "trends_version.csv"):
        w.write({"app_version": r.get("app_version"), "issue_id": r.get("issue_id"),
                 "complaints": integer(r.get("complaints")),
                 "completed_in_version": integer(r.get("completed_in_version")), "share": number(r.get("share"))})
    return n_month, w.close()


def public_quotes(final: dict) -> list[dict]:
    """The memo's quotes with only issue id and quote text. Review ids never leave the run directory."""
    quotes = final.get("quotes")
    if not isinstance(quotes, list):
        raise SystemExit("memo.json final has no `quotes` list; this run predates the s2 schema. "
                         "Rebuild it with the current pipeline.")
    return [{"issue_id": str(q.get("issue_id") or ""), "quote": text(q.get("quote") or "")}
            for q in quotes if isinstance(q, dict) and q.get("quote")]


def require_segments(run: Path, con: sqlite3.Connection) -> None:
    """Older runs lack the free-tier and plan fields; refuse them with a clear message instead of guessing."""
    missing = [name for name in SEGMENT_FILES if not (run / name).exists()]
    cols = {row[1] for row in con.execute("PRAGMA table_info(records)")}
    missing += [f"records.{c} column in pipeline.sqlite" for c in SEGMENT_COLUMNS if c not in cols]
    if missing:
        raise SystemExit(f"{run.name} is missing {', '.join(missing)}. The dashboard needs an s2-schema run "
                         "(free_tier_limit and plan per review, segment files); rebuild the run with the current "
                         "pipeline.")


def build_segments(run: Path, out: Path) -> tuple[int, int]:
    w = TableWriter(out, "segment_ranking")
    for r in stream_csv(run / "ranking_segments.csv"):
        lens = LENS_KEYS.get((r.get("lens") or "").strip())
        if lens is None:
            raise SystemExit(f"ranking_segments.csv has an unknown lens {r.get('lens')!r}; "
                             f"expected one of {sorted(LENS_KEYS)}")
        w.write({"lens": lens, "rank": integer(r.get("rank")), "issue_id": r.get("issue_id"),
                 "complaint_count": integer(r.get("complaint_count")), "severity_sum": integer(r.get("severity_sum")),
                 "mean_severity": number(r.get("mean_severity")), "priority_score": number(r.get("priority_score"))})
    n_ranking = w.close()
    w = TableWriter(out, "segment_areas")
    for r in stream_csv(run / "segments_area.csv"):
        w.write({"area": r.get("area"), "free_tier_limit": boolean(r.get("free_tier_limit")),
                 "plan": r.get("plan") or None, "complaints": integer(r.get("complaints")),
                 "severity_sum": integer(r.get("severity_sum"))})
    return n_ranking, w.close()


def build_memo(run: Path, out: Path) -> tuple[int, int, int]:
    memo = read_json(run / "memo.json") or {}
    facts = memo.get("facts") or {}
    w = TableWriter(out, "areas")
    for pos, (area, a) in enumerate((facts.get("product_areas") or {}).items(), start=1):
        w.write({"area": area, "position": pos, "topics": jsonb(a.get("topics") or []),
                 "complaints": integer(a.get("complaints")), "severity_sum": integer(a.get("severity_sum")),
                 "share_pct": number(a.get("share_of_all_complaints_pct")),
                 "in_question": boolean(area != "outside the question")})
    n_areas = w.close()

    final = memo.get("final") or {}
    w = TableWriter(out, "memo")
    if final:
        w.write({"id": 1, "question": facts.get("question"), "ranking_rule": facts.get("ranking_rule"),
                 "priority_area": final.get("priority_area"), "headline": final.get("headline"),
                 "memo_markdown": text(final.get("memo_markdown")),
                 "quotes": jsonb(public_quotes(final)),
                 "revised": boolean(bool(memo.get("revised"))), "critique": jsonb(memo.get("critique")),
                 "final_critique": jsonb(memo.get("final_critique")),
                 "final_problems": jsonb(memo.get("final_problems") or []),
                 "draft_problems": jsonb(memo.get("draft_problems") or [])})
    else:
        warn("memo.json has no final memo; memo table left empty")
    n_memo = w.close()

    claims = read_csv(run / "claims.csv") if (run / "claims.csv").exists() else final.get("claims") or []
    w = TableWriter(out, "claims")
    for pos, c in enumerate(claims, start=1):
        w.write({"claim_id": c.get("claim_id"), "position": pos, "issue_id": c.get("issue_id"),
                 "metric": c.get("metric"), "value": str(c.get("value"))})
    return n_areas, n_memo, w.close()


def build_stages(con: sqlite3.Connection, out: Path) -> int:
    w = TableWriter(out, "stages")
    cur = con.execute(
        """
        SELECT role, coalesce(step, ''), group_concat(DISTINCT provider), group_concat(DISTINCT model),
               group_concat(DISTINCT effort), group_concat(DISTINCT tier), count(*),
               sum(CASE WHEN outcome = 'succeeded' THEN 0 ELSE 1 END),
               sum(input_tokens), sum(cached_input_tokens), sum(cache_write_tokens), sum(output_tokens),
               sum(reasoning_tokens), sum(cost_usd), sum(duration_ms), min(started_at), max(started_at)
        FROM calls GROUP BY role, coalesce(step, '') ORDER BY min(started_at)
        """
    )
    for row in cur:
        (role, step, provider, model, effort, tier, calls, failed, inp, cached, cwrite, outp, reasoning,
         cost, duration, first, last) = row
        w.write({"role": role, "step": step, "provider": provider, "model": model, "effort": effort or None,
                 "tier": tier, "calls": calls, "failed": failed, "input_tokens": integer(inp, 0, 2**62),
                 "cached_input_tokens": integer(cached, 0, 2**62), "cache_write_tokens": integer(cwrite, 0, 2**62),
                 "output_tokens": integer(outp, 0, 2**62), "reasoning_tokens": integer(reasoning, 0, 2**62),
                 "cost_usd": number(cost) or 0.0, "duration_ms": integer(duration, 0, 2**62),
                 "first_started_at": epoch_iso(first), "last_started_at": epoch_iso(last)})
    return w.close()


DIAGNOSIS_FILES = ("diagnosis.json", "diagnosis_symptoms.csv", "diagnosis_context.csv", "diagnosis_customers.csv",
                   "diagnosis_verify.json")


def plan_gated_ids(repo: Path) -> set[str]:
    """Symptoms the free tier restricts by design, from the approved catalog (prompts/symptoms.json)."""
    path = repo / "prompts" / "symptoms.json"
    catalog = read_json(path) if path.exists() else None
    if not isinstance(catalog, dict) or not isinstance(catalog.get("plan_gated"), list):
        warn(f"{path} has no plan_gated list; symptoms without a plan_gated column count as defects")
        return set()
    return {str(x) for x in catalog["plan_gated"]}


def build_diagnosis(run: Path, out: Path, repo: Path) -> tuple[int, int, int, int]:
    """Diagnosis tables from the diagnose stage's outputs. A run without them still builds, with empty tables.

    `plan_gated` comes from the CSV column when the run has it, else from the catalog's plan_gated list."""
    missing = [name for name in DIAGNOSIS_FILES if not (run / name).exists()]
    if missing:
        warn(f"{run.name} has no diagnosis outputs ({', '.join(missing)} missing); "
             "diagnosis tables left empty or partial")
    present = lambda name: name not in missing  # noqa: E731
    diagnosis = (read_json(run / "diagnosis.json") if present("diagnosis.json") else None) or {}
    catalog = {(area, s.get("symptom_id")): s for area, items in (diagnosis.get("areas") or {}).items()
               for s in (items or []) if isinstance(s, dict)}

    w = TableWriter(out, "diagnosis_symptoms")
    rows = read_csv(run / "diagnosis_symptoms.csv") if present("diagnosis_symptoms.csv") else []
    gated = None if rows and "plan_gated" in rows[0] else (plan_gated_ids(repo) if rows else set())
    for r in rows:
        area, sid = r.get("area"), r.get("symptom_id")
        w.write({
            "area": area, "symptom_id": sid, "name": r.get("symptom_name") or None,
            "definition": text((catalog.get((area, sid)) or {}).get("definition")) or None,
            "complaints": integer(r.get("complaints")) or 0, "severity_sum": integer(r.get("severity_sum")) or 0,
            "mean_severity": number(r.get("mean_severity")),
            "paying_complaints": integer(r.get("paying_complaints")) or 0,
            "paying_severity_sum": integer(r.get("paying_severity_sum")) or 0,
            "first6_complaints": integer(r.get("first6_complaints")) or 0,
            "last6_complaints": integer(r.get("last6_complaints")) or 0,
            "last6_severity_sum": integer(r.get("last6_severity_sum")) or 0,
            "likes_sum": integer(r.get("likes_sum")) or 0, "cancellations": integer(r.get("cancellations")) or 0,
            "top_context": r.get("top_context") or None, "top_context_share_pct": number(r.get("top_context_share_pct")),
            "top_trigger": r.get("top_trigger") or None, "top_trigger_share_pct": number(r.get("top_trigger_share_pct")),
            "top_version": r.get("top_version") or None, "top_version_share_pct": number(r.get("top_version_share_pct")),
            "overall_share_pct": number(r.get("overall_share_pct")),
            "top_version_reviews": integer(r.get("top_version_reviews")),
            "plan_gated": (boolean(r.get("plan_gated")) or "f") if gated is None else boolean(sid in gated),
        })
    n_symptoms = w.close()

    w = TableWriter(out, "diagnosis_context")
    for r in (read_csv(run / "diagnosis_context.csv") if present("diagnosis_context.csv") else []):
        w.write({"area": r.get("area"), "context": r.get("context") or None, "trigger": r.get("trigger") or None,
                 "complaints": integer(r.get("complaints")), "severity_sum": integer(r.get("severity_sum"))})
    n_context = w.close()

    w = TableWriter(out, "diagnosis_customers")
    for r in (read_csv(run / "diagnosis_customers.csv") if present("diagnosis_customers.csv") else []):
        w.write({k: (r.get(k) if k == "area" else integer(r.get(k))) for k in w.cols})
    n_customers = w.close()

    w = TableWriter(out, "diagnosis_quotes")
    for (area, sid), s in catalog.items():
        for rank, q in enumerate([q for q in s.get("quotes") or [] if isinstance(q, dict) and q.get("quote")],
                                 start=1):
            w.write({"area": area, "symptom_id": sid, "rank": rank, "quote": text(q.get("quote")),
                     "likes": integer(q.get("likes"))})
    return n_symptoms, n_context, n_customers, w.close()


def build_kv(run: Path, repo: Path, out: Path) -> int:
    kv: dict[str, object] = {}
    for key, path in [("run_summary", run / "run_summary.json"), ("ingestion", run / "ingestion.json"),
                      ("verify_planted", run / "verify_planted.json"),
                      ("classifier_probe", repo / "evals/classifier_probe/summary.json"),
                      ("cost_pilot", repo / "cost/pilot_runs.json")]:
        value = read_json(path)
        if value is not None:
            kv[key] = value

    report = read_json(run / "verify_report.json")
    if report is not None:
        disagreements = report.get("disagreements") or []
        kv["verify_report"] = {
            "n": report.get("n"), "agreement": report.get("agreement"),
            "all_three_agree": report.get("all_three_agree"), "severity_mae": report.get("severity_mae"),
            "topic_confusion": report.get("topic_confusion"), "verifier": report.get("verifier"),
            "n_disagreements": len(disagreements), "disagreements": disagreements[:20],
        }

    golden = read_json(repo / "evals/golden/results.json")
    if golden is not None:  # per-case rows are dropped; the scores are what the dashboard shows
        kv["golden"] = {label_set: {model: {k: v for k, v in scores.items() if k != "cases"}
                                    for model, scores in (models or {}).items() if isinstance(scores, dict)}
                        for label_set, models in golden.items() if isinstance(models, dict)}

    injection = read_json(repo / "evals/injection/results.json")
    if injection is not None:
        haiku = injection.get("haiku") or {}
        kv["injection"] = {
            "cases": [{k: v for k, v in c.items() if not k.endswith(("request_id", "input_tokens"))}
                      for c in injection.get("cases") or []],
            "haiku_call": {k: haiku.get(k) for k in ("ok", "stop_reason", "valid_lines", "invalid_lines")},
            "neighbours": len(injection.get("neighbours") or []),
        }

    diagnosis = read_json(run / "diagnosis.json") if (run / "diagnosis.json").exists() else None
    if diagnosis is not None:
        kv["diagnosis_catalog_version"] = diagnosis.get("catalog_version")
        kv["diagnosis_coverage"] = diagnosis.get("coverage")
        if isinstance(diagnosis.get("concrete"), dict):  # per-area concrete-defect totals; older runs lack it
            kv["diagnosis_concrete"] = diagnosis["concrete"]
    dverify = read_json(run / "diagnosis_verify.json") if (run / "diagnosis_verify.json").exists() else None
    if dverify is not None:  # agreement only; the disagreement rows carry review ids and text
        kv["diagnosis_verify"] = {k: dverify.get(k) for k in ("n", "agreement", "by_area", "verifier",
                                                              "disagreements_total")}

    w = TableWriter(out, "kv")
    for key in sorted(kv):
        w.write({"key": key, "value": jsonb(kv[key])})
    return w.close()


# ---------------------------------------------------------------- main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True, type=Path, help="finalized run directory, e.g. runs/pilot-100-t2")
    ap.add_argument("--out", required=True, type=Path, help="bundle directory to write")
    ap.add_argument("--repo", type=Path, default=REPO, help="repo root holding evals/ and cost/")
    args = ap.parse_args(argv)

    run, out = args.run.resolve(), args.out.resolve()
    db_path = run / "pipeline.sqlite"
    if not db_path.exists():
        ap.error(f"{db_path} not found")
    summary = read_json(run / "run_summary.json") or {}
    run_id = summary.get("run_id") or run.name
    out.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    try:
        require_segments(run, con)
        counts: dict[str, int] = {}
        counts["reviews"], complaints, cancellations = build_reviews(con, out)
        counts["stages"] = build_stages(con, out)
    finally:
        con.close()
    counts["issues"] = build_issues(run, out, complaints, cancellations)
    counts["trends_month"], counts["trends_version"] = build_trends(run, out)
    counts["segment_ranking"], counts["segment_areas"] = build_segments(run, out)
    counts["areas"], counts["memo"], counts["claims"] = build_memo(run, out)
    (counts["diagnosis_symptoms"], counts["diagnosis_context"], counts["diagnosis_customers"],
     counts["diagnosis_quotes"]) = build_diagnosis(run, out, args.repo.resolve())
    counts["kv"] = build_kv(run, args.repo.resolve(), out)
    (out / "schema.sql").write_text(schema_sql(run_id), encoding="utf-8")

    print(f"bundle for run {run_id} -> {out}")
    for name in TABLES:
        print(f"  {name:<19} {counts[name]:>9,} rows")
    print(f"  complaint or cancellation reviews: {sum(complaints.values()):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
