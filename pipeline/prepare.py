"""Stage 1 · code only: stream the CSV into SQLite, hash rows, dedup exact texts, build the backlog."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from tools.check_submission import csv_rows, profile, row_sha, sha

from .db import meta_get, meta_set, tx

CHUNK_ROWS = 20_000


def prepare(conn, csv_path: Path, run_dir: Path) -> dict:
    """Load every row once. Idempotent: a second call on the same file returns the saved report."""
    csv_path = Path(csv_path)
    started = time.time()
    file_sha = sha(csv_path)
    known = meta_get(conn, "input_sha256")
    if known == file_sha:
        return json.loads(meta_get(conn, "ingestion_report"))
    if known:
        raise ValueError(f"This run already holds a different input file ({known[:12]}…); use a new --run id")

    seen: dict[str, int] = {}
    reviews, texts = [], []
    rows = duplicates = 0

    def flush():
        nonlocal duplicates
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO reviews VALUES(?,?,?,?,?,?,?,?)", reviews)
        duplicates += len(reviews) - (conn.total_changes - before)
        conn.executemany("INSERT OR IGNORE INTO texts(text_sha, text, n_chars, origin_review_id) VALUES(?,?,?,?)", texts)
        reviews.clear()
        texts.clear()

    with tx(conn):
        for row in csv_rows(csv_path):  # the grader's own reader: strict CSV, multiline text preserved
            text = row["review_text"]
            # Dedup key is the exact text: no trimming, no case folding, no Unicode normalisation.
            text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest() if text.strip() else None
            reviews.append((row["review_id"], rows, row_sha(row), text_sha, row["review_rating"],
                            row["review_likes"], row["app_version"], row["review_timestamp"]))
            if text_sha:
                if text_sha not in seen:
                    seen[text_sha] = 0
                    texts.append((text_sha, text, len(text), row["review_id"]))
                seen[text_sha] += 1
            rows += 1
            if len(reviews) >= CHUNK_ROWS:
                flush()
        flush()
        conn.executemany("UPDATE texts SET n_reviews=? WHERE text_sha=?",
                         ((n, key) for key, n in seen.items() if n > 1))

        one = lambda sql: conn.execute(sql).fetchone()[0]
        nonempty = one("SELECT COUNT(*) FROM reviews WHERE text_sha IS NOT NULL")
        unique = one("SELECT COUNT(*) FROM texts")
        lengths = [r[0] for r in conn.execute("SELECT n_chars FROM texts ORDER BY n_chars")]
        pick = lambda p: lengths[min(len(lengths) - 1, int(len(lengths) * p))] if lengths else 0
        report = {
            "input_file": csv_path.name, "input_bytes": csv_path.stat().st_size, "input_sha256": file_sha,
            "rows": rows, "duplicate_review_ids_skipped": duplicates,
            "nonempty_texts": nonempty, "empty_text_quarantined": rows - duplicates - nonempty,
            "missing_app_version": one("SELECT COUNT(*) FROM reviews WHERE trim(app_version)=''"),
            "unique_exact_texts": unique, "rows_reusing_a_result": nonempty - unique,
            "unique_text_chars": {"total": sum(lengths), "median": pick(.5), "p90": pick(.9),
                                  "p99": pick(.99), "max": lengths[-1] if lengths else 0},
            "top_repeated_texts": [{"text": r["text"][:60], "reviews": r["n_reviews"]} for r in conn.execute(
                "SELECT text, n_reviews FROM texts ORDER BY n_reviews DESC, text_sha LIMIT 10")],
            "reviews_by_rating": {r[0]: r[1] for r in conn.execute(
                "SELECT review_rating, COUNT(*) FROM reviews GROUP BY 1 ORDER BY 1")},
            "reviews_by_month": {r[0]: r[1] for r in conn.execute(
                "SELECT substr(review_timestamp,1,7), COUNT(*) FROM reviews GROUP BY 1 ORDER BY 1")},
            "model_calls": 0,
        }
        report["seconds"] = round(time.time() - started, 2)
        meta_set(conn, "input_sha256", file_sha)
        meta_set(conn, "input_path", str(csv_path))
        meta_set(conn, "ingestion_report", json.dumps(report, ensure_ascii=False))

    run_dir.mkdir(parents=True, exist_ok=True)
    write = lambda name, value: (run_dir / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write("ingestion_report.json", report)
    # The grading profile comes from the provided helper so serialisation cannot drift.
    write("ingestion.json", profile(csv_path))
    write("data_manifest.json", {"file": csv_path.name, "bytes": report["input_bytes"], "sha256": file_sha,
                                 "rows": rows, "source": "BwandoWando, 3.4 Million Spotify Google Store Reviews, v2 (CC0)"})
    return report
