"""SQLite state: the review table, the backlog of unique texts, requests and the call log."""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS reviews(
  review_id TEXT PRIMARY KEY, row_num INTEGER NOT NULL, source_sha256 TEXT NOT NULL,
  text_sha TEXT,                         -- NULL when the review text is empty
  review_rating TEXT, review_likes TEXT, app_version TEXT, review_timestamp TEXT
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS reviews_text ON reviews(text_sha);

-- The backlog: one row per exact distinct text.
-- status: pending -> leased -> fallback_pending -> completed | quarantined
CREATE TABLE IF NOT EXISTS texts(
  text_sha TEXT PRIMARY KEY, text TEXT NOT NULL, n_chars INTEGER NOT NULL,
  n_reviews INTEGER NOT NULL DEFAULT 1, origin_review_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  request_key TEXT, result TEXT, reason TEXT, doubt TEXT, step TEXT, updated_at REAL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS texts_status ON texts(status, attempts, n_chars, text_sha);

-- One row per model request we built (at most 50 texts). status: built -> submitted -> done | failed
CREATE TABLE IF NOT EXISTS requests(
  request_key TEXT PRIMARY KEY, role TEXT NOT NULL, step TEXT NOT NULL, status TEXT NOT NULL,
  provider_batch TEXT, invocation INTEGER NOT NULL, n_items INTEGER NOT NULL,
  est_input_tokens INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL,
  reserved_usd REAL NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_status ON requests(status);
CREATE TABLE IF NOT EXISTS request_items(
  request_key TEXT NOT NULL, idx INTEGER NOT NULL, text_sha TEXT NOT NULL,
  PRIMARY KEY(request_key, idx)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS provider_batches(
  provider_batch TEXT PRIMARY KEY, status TEXT NOT NULL, n_requests INTEGER NOT NULL,
  est_input_tokens INTEGER NOT NULL, submitted_at REAL NOT NULL, finished_at REAL
);

-- One row per attempted model call, successful or not.
CREATE TABLE IF NOT EXISTS calls(
  request_id TEXT PRIMARY KEY, request_key TEXT, role TEXT NOT NULL, step TEXT, provider TEXT,
  model TEXT NOT NULL, effort TEXT, tier TEXT, label_config TEXT, invocation INTEGER NOT NULL,
  outcome TEXT NOT NULL, error TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0, usage_available INTEGER NOT NULL DEFAULT 1,
  cost_usd REAL NOT NULL DEFAULT 0, started_at REAL, duration_ms INTEGER,
  stop_reason TEXT, n_valid INTEGER, n_invalid INTEGER, review_ids TEXT NOT NULL, output_text TEXT
);

-- The verifier's blind labels for the declared sample, one row per unique text.
CREATE TABLE IF NOT EXISTS verifications(
  text_sha TEXT PRIMARY KEY, origin_review_id TEXT NOT NULL, request_id TEXT, topic TEXT, intent TEXT,
  severity INTEGER, sentiment REAL, confidence TEXT, probabilities TEXT, created_at REAL,
  free_tier_limit INTEGER, plan TEXT
) WITHOUT ROWID;

-- The diagnosis backlog: one row per diagnosable unique text (a complaint or cancellation in access,
-- usability, playback, billing or support that is not about a free-tier limit). Copies inherit it by text_sha.
-- status: pending -> leased -> completed | failed. A failed diagnosis never touches the review's record.
CREATE TABLE IF NOT EXISTS diagnoses(
  text_sha TEXT PRIMARY KEY, area TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, reason TEXT, request_key TEXT,
  symptom_id TEXT, context TEXT, trigger TEXT, long_time_user INTEGER, quote TEXT, created_at REAL NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS diagnoses_status ON diagnoses(status, attempts, text_sha);

-- The blind check of a declared sample of diagnoses, one row per unique text.
CREATE TABLE IF NOT EXISTS diagnosis_checks(
  text_sha TEXT PRIMARY KEY, origin_review_id TEXT NOT NULL, request_id TEXT, symptom_id TEXT, context TEXT,
  confidence TEXT, created_at REAL
) WITHOUT ROWID;

-- Outputs of the group and memo roles, keyed by a hash of their exact inputs and settings,
-- so an unchanged rerun reuses them instead of calling a model again.
CREATE TABLE IF NOT EXISTS artifacts(
  kind TEXT NOT NULL, input_hash TEXT NOT NULL, output TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(kind, input_hash)
) WITHOUT ROWID;
"""


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


@contextlib.contextmanager
def tx(conn: sqlite3.Connection):
    """Atomic write: either every statement lands or none does."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def backlog_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM texts GROUP BY status")}


def diagnosis_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM diagnoses GROUP BY status")}
