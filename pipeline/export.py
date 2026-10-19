"""Saved handoffs: join results back onto every source id, then write the grading export."""
from __future__ import annotations

import csv
import gzip
import json
import shutil
import time
from pathlib import Path

from .config import COMPLAINT_INTENTS
from .db import backlog_counts, meta_get, tx
from .rank import rank, write_ranking

AUDIT_VERSION = "a5-audit-v1"
LABELS = ("topic", "intent", "sentiment", "severity", "entities", "evidence_quote", "needs_review")
DIAGNOSIS_KEYS = ("symptom", "context", "trigger", "long_time_user")  # only on diagnosed records


def _dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _csv(path: Path, header: list[str], rows) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def write_checkpoint(conn, run_dir: Path, invocation: int, ended: str) -> Path:
    """A snapshot of every review id whose classification is final at this moment."""
    ids = [r[0] for r in conn.execute(
        "SELECT r.review_id FROM reviews r JOIN texts t ON t.text_sha=r.text_sha "
        "WHERE t.status='completed' ORDER BY r.row_num")]
    folder = run_dir / "checkpoints"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"inv{invocation}.json"
    path.write_text(json.dumps({"invocation": invocation, "ended": ended, "saved_at": time.time(),
                                "backlog": backlog_counts(conn), "completed_count": len(ids),
                                "completed_ids": ids}), encoding="utf-8")
    return path


def finalize(conn, run_dir: Path, tax: dict) -> dict:
    """Code only. Every source review id leaves here as exactly one completed or quarantined record."""
    label_config = meta_get(conn, "label_config")

    with tx(conn):  # a review-level table for the trend SQL and, later, the dashboard database
        conn.execute("DROP TABLE IF EXISTS records")
        conn.execute("""
            CREATE TABLE records AS
            SELECT r.review_id, r.row_num, r.review_rating, r.app_version, substr(r.review_timestamp,1,7) AS month,
                   CASE WHEN t.status='completed' THEN 'completed' ELSE 'quarantined' END AS status,
                   CASE WHEN r.text_sha IS NULL THEN 'empty_review_text'
                        WHEN t.status='quarantined' THEN t.reason
                        WHEN t.status!='completed' THEN 'unprocessed:' || t.status END AS reason,
                   CASE WHEN t.status='completed' THEN json_extract(t.result,'$.topic') END AS topic,
                   CASE WHEN t.status='completed' THEN json_extract(t.result,'$.subtopic') END AS subtopic,
                   CASE WHEN t.status='completed' THEN json_extract(t.result,'$.intent') END AS intent,
                   CASE WHEN t.status='completed' THEN json_extract(t.result,'$.severity') END AS severity,
                   CASE WHEN t.status='completed' THEN json_extract(t.result,'$.sentiment') END AS sentiment,
                   CASE WHEN t.status='completed' THEN json_extract(t.result,'$.needs_review') END AS needs_review,
                   CASE WHEN t.status='completed' THEN json_extract(t.result,'$.free_tier_limit') END AS free_tier_limit,
                   CASE WHEN t.status='completed' THEN json_extract(t.result,'$.plan') END AS plan,
                   CASE WHEN t.status='completed' AND t.origin_review_id!=r.review_id THEN t.origin_review_id END AS cache_source_id
            FROM reviews r LEFT JOIN texts t ON t.text_sha=r.text_sha""")
        conn.execute("CREATE INDEX records_issue ON records(intent, topic, subtopic)")

    counts = {"completed": 0, "quarantined": 0, "cache_reuse": 0, "complaints": 0}
    quarantine, membership = [], []
    with gzip.open(run_dir / "records.jsonl.gz", "wt", encoding="utf-8") as out:
        for row in conn.execute(
                "SELECT r.review_id, r.source_sha256, r.text_sha, t.status, t.result, t.reason, t.attempts, "
                "t.origin_review_id, t.step, d.symptom_id, d.context, d.trigger, d.long_time_user FROM reviews r "
                "LEFT JOIN texts t ON t.text_sha=r.text_sha "
                "LEFT JOIN diagnoses d ON d.text_sha=r.text_sha AND d.status='completed' ORDER BY r.row_num"):
            base = {"review_id": row["review_id"], "source_sha256": row["source_sha256"]}
            if row["status"] == "completed":
                result = json.loads(row["result"])
                record = {**base, "status": "completed", **{k: result[k] for k in LABELS},
                          "label_config": label_config, "subtopic": result["subtopic"], "step": row["step"]}
                if "plan" in result:  # schema s2 segmentation fields, extra keys beside the contract's
                    record.update({"free_tier_limit": result["free_tier_limit"], "plan": result["plan"]})
                if row["symptom_id"] is not None:  # diagnosed: copies inherit the origin's diagnosis by text
                    record.update({"symptom": row["symptom_id"], "context": row["context"], "trigger": row["trigger"],
                                   "long_time_user": bool(row["long_time_user"])})
                if row["origin_review_id"] != row["review_id"]:
                    record["cache_source_id"] = row["origin_review_id"]
                    counts["cache_reuse"] += 1
                if result["intent"] in COMPLAINT_INTENTS:
                    membership.append((f"{result['topic']}.{result['subtopic']}", row["review_id"]))
                counts["completed"] += 1
            else:
                reason = ("empty_review_text" if row["text_sha"] is None else row["reason"]
                          if row["status"] == "quarantined" else f"unprocessed:{row['status']}")
                record = {**base, "status": "quarantined", "reason": reason}
                quarantine.append({**record, "attempts": row["attempts"] or 0})
                counts["quarantined"] += 1
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    counts["complaints"] = len(membership)

    with (run_dir / "quarantine.jsonl").open("w", encoding="utf-8") as f:
        for record in quarantine:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    membership.sort()
    _csv(run_dir / "membership.csv", ["issue_id", "review_id"], membership)
    from .group import write_issues
    write_issues(run_dir, tax)
    ranking = rerank(run_dir)
    _trends(conn, run_dir)
    _segments(conn, run_dir)
    from .symptom_tables import write_diagnosis
    write_diagnosis(conn, run_dir)  # regenerated whenever this run has diagnoses; a no-op otherwise

    usage = conn.execute(
        "SELECT COUNT(*) calls, COALESCE(SUM(outcome='failed'),0) failed, COALESCE(SUM(input_tokens),0) input_tokens, "
        "COALESCE(SUM(cached_input_tokens),0) cached_input_tokens, COALESCE(SUM(cache_write_tokens),0) cache_write_tokens, "
        "COALESCE(SUM(output_tokens),0) output_tokens, "
        "COALESCE(SUM(cost_usd),0) cost_usd, COALESCE(MIN(usage_available),1) usage_available FROM calls").fetchone()
    with (run_dir / "run_log.jsonl").open("w", encoding="utf-8") as f:
        for call in conn.execute("SELECT * FROM calls ORDER BY started_at, request_id"):
            f.write(json.dumps({**dict(call), "review_ids": json.loads(call["review_ids"])}, ensure_ascii=False) + "\n")
    summary = {
        "run_id": run_dir.name, "input_sha256": meta_get(conn, "input_sha256"), "label_config": label_config,
        "settings": json.loads(meta_get(conn, "settings", "{}")), "taxonomy_version": tax["version"],
        "source_rows": counts["completed"] + counts["quarantined"], **counts, "backlog": backlog_counts(conn),
        "unique_texts": conn.execute("SELECT COUNT(*) FROM texts").fetchone()[0],
        "fallback_texts": conn.execute("SELECT COUNT(*) FROM texts WHERE step='fallback'").fetchone()[0],
        "needs_review_texts": conn.execute(
            "SELECT COUNT(*) FROM texts WHERE status='completed' AND json_extract(result,'$.needs_review')").fetchone()[0],
        "issues_ranked": len(ranking), "invocations": int(meta_get(conn, "invocation", "0")),
        "usage": dict(usage), "finalized_at": time.time(),
    }
    _dump(run_dir / "run_summary.json", summary)
    return summary


def rerank(run_dir: Path) -> list[dict]:
    """Regenerate ranking.csv from the saved records and membership. No database, no model."""
    rows = rank(run_dir / "records.jsonl.gz", run_dir / "membership.csv")
    write_ranking(rows, run_dir / "ranking.csv")
    return rows


AREA_SQL = ("CASE WHEN topic IN ('billing','support') THEN 'billing/support' "
            "WHEN topic IN ('downloads','catalog','other') THEN 'outside the question' ELSE topic END")
LENSES = {  # the baseline ranking stays the contract's; these are additional views, documented as such
    "all complaints (baseline)": "1",
    "without free-tier limits": "coalesce(free_tier_limit, 0) = 0",
    "paying reviewers only": "plan = 'paid'",
}


def _segments(conn, run_dir: Path) -> None:
    """Free versus premium: the same ranking rule applied to segments of the complaints. SQL only."""
    if conn.execute("SELECT COUNT(*) FROM records WHERE plan IS NOT NULL").fetchone()[0] == 0:
        return  # a schema-s1 run has no segmentation fields
    complaint = "status='completed' AND intent IN ('complaint','cancellation')"
    rows = []
    for lens, where in LENSES.items():
        issues = conn.execute(f"""
            SELECT topic || '.' || subtopic AS issue_id, COUNT(*) AS n, SUM(severity) AS s FROM records
            WHERE {complaint} AND {where} GROUP BY issue_id ORDER BY s DESC, issue_id""").fetchall()
        rows += [[lens, rank, r["issue_id"], r["n"], r["s"], f"{r['s'] / r['n']:.6f}", r["s"]]
                 for rank, r in enumerate(issues, 1)]
    _csv(run_dir / "ranking_segments.csv",
         ["lens", "rank", "issue_id", "complaint_count", "severity_sum", "mean_severity", "priority_score"], rows)
    _csv(run_dir / "segments_area.csv",
         ["area", "free_tier_limit", "plan", "complaints", "severity_sum"],
         ([r["area"], r["lim"], r["plan"], r["n"], r["s"]] for r in conn.execute(f"""
             SELECT {AREA_SQL} AS area, coalesce(free_tier_limit, 0) AS lim, plan, COUNT(*) AS n, SUM(severity) AS s
             FROM records WHERE {complaint} GROUP BY area, lim, plan ORDER BY area, lim, plan""")))


def _trends(conn, run_dir: Path) -> None:
    """Complaint share per issue in comparable periods, with denominators. SQL only."""
    complaint = "intent IN ('complaint','cancellation')"
    months = [r[0] for r in conn.execute("SELECT DISTINCT month FROM records ORDER BY 1")]
    partial = {months[0], months[-1]} if months else set()
    _csv(run_dir / "trends_month.csv",
         ["month", "issue_id", "complaints", "completed_in_month", "share", "partial_month"],
         ([r["month"], r["issue_id"], r["complaints"], r["denominator"],
           f"{r['complaints'] / r['denominator']:.6f}", int(r["month"] in partial)]
          for r in conn.execute(f"""
              SELECT c.month, c.topic || '.' || c.subtopic AS issue_id, COUNT(*) AS complaints, d.n AS denominator
              FROM records c JOIN (SELECT month, COUNT(*) n FROM records WHERE status='completed' GROUP BY month) d
                ON d.month=c.month
              WHERE c.status='completed' AND c.{complaint} GROUP BY c.month, issue_id ORDER BY c.month, issue_id""")))
    # 8.8.76.667 -> 8.8, so versions are comparable release lines rather than thousands of builds
    conn.create_function("release_line", 1, lambda v: ".".join(v.split(".")[:2]) if v.strip() else "missing")
    version = "release_line(app_version)"
    _csv(run_dir / "trends_version.csv",
         ["app_version", "issue_id", "complaints", "completed_in_version", "share"],
         ([r["v"], r["issue_id"], r["complaints"], r["denominator"], f"{r['complaints'] / r['denominator']:.6f}"]
          for r in conn.execute(f"""
              WITH v AS (SELECT *, {version} AS v FROM records WHERE status='completed')
              SELECT c.v, c.topic || '.' || c.subtopic AS issue_id, COUNT(*) AS complaints, d.n AS denominator
              FROM v c JOIN (SELECT v, COUNT(*) n FROM v GROUP BY v) d ON d.v=c.v
              WHERE c.{complaint} GROUP BY c.v, issue_id ORDER BY c.v, issue_id""")))


def write_records(conn, source: Path, target: Path) -> int:
    """Copy records.jsonl.gz with the diagnosis keys as they stand now, on diagnosed records only.

    A run that never diagnosed is copied byte for byte. Returns how many records carry the keys.
    """
    if not meta_get(conn, "diagnose_config"):
        shutil.copyfile(source, target)
        return 0
    from .diagnose import diagnosed_record_keys
    keys, tagged = diagnosed_record_keys(conn), 0
    with gzip.open(source, "rt", encoding="utf-8") as src, gzip.open(target, "wt", encoding="utf-8") as out:
        for line in src:
            record = json.loads(line)
            for key in DIAGNOSIS_KEYS:
                record.pop(key, None)
            if record["status"] == "completed" and record["review_id"] in keys:
                record.update(keys[record["review_id"]])
                tagged += 1
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    return tagged


def export_grading(conn, run_dir: Path, out: Path, before_invocation: int = 1) -> dict:
    """Write the standard grading/ folder from the saved artifacts of a finalized run."""
    out.mkdir(parents=True, exist_ok=True)
    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    _dump(out / "run.json", {
        "version": AUDIT_VERSION, "analysis_count": summary["source_rows"],
        "analysis_sha256": summary["input_sha256"], "classification_input_fields": ["review_text"],
        "allow_multi_issue": False, "run_id": summary["run_id"], "label_config": summary["label_config"]})
    for name in ("ingestion.json", "membership.csv", "ranking.csv"):
        shutil.copyfile(run_dir / name, out / name)
    write_records(conn, run_dir / "records.jsonl.gz", out / "records.jsonl.gz")
    claims = run_dir / "claims.csv"
    if claims.exists():
        shutil.copyfile(claims, out / "claims.csv")
    else:  # the memo stage writes the real claims
        _csv(out / "claims.csv", ["claim_id", "issue_id", "metric", "value"], [])

    calls = 0
    with gzip.open(out / "calls.jsonl.gz", "wt", encoding="utf-8") as f:
        for c in conn.execute("SELECT * FROM calls ORDER BY started_at, request_id"):
            f.write(json.dumps({
                "request_id": c["request_id"], "role": c["role"], "review_ids": json.loads(c["review_ids"]),
                "model": c["model"], "phase": "initial" if c["invocation"] <= before_invocation else "resume",
                "outcome": c["outcome"], "label_config": c["label_config"], "input_tokens": c["input_tokens"],
                "output_tokens": c["output_tokens"], "step": c["step"], "effort": c["effort"], "tier": c["tier"],
                "cached_input_tokens": c["cached_input_tokens"], "cache_write_tokens": c["cache_write_tokens"],
                "stop_reason": c["stop_reason"], "usage_available": bool(c["usage_available"]),
                "error": c["error"], "duration_ms": c["duration_ms"]}, ensure_ascii=False) + "\n")
            calls += 1

    checkpoints = sorted((run_dir / "checkpoints").glob("inv*.json"), key=lambda p: int(p.stem[3:]))
    before = next((p for p in checkpoints if int(p.stem[3:]) == before_invocation), None)
    resumed = bool(before and checkpoints and checkpoints[-1] != before)
    if resumed:
        # "after" is the state at export time: everything final, including the fallback step.
        final_ids = [r[0] for r in conn.execute(
            "SELECT r.review_id FROM reviews r JOIN texts t ON t.text_sha=r.text_sha "
            "WHERE t.status='completed' ORDER BY r.row_num")]
        _dump(out / "checkpoint_before.json",
              {"completed_ids": json.loads(before.read_text(encoding="utf-8"))["completed_ids"]})
        _dump(out / "checkpoint_after.json", {"completed_ids": final_ids})
    return {"out": str(out), "calls": calls, "resume_evidence": resumed,
            "claims": "from memo" if claims.exists() else "empty (memo stage not run)"}
