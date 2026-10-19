"""Load a dashboard bundle into Postgres (Neon) in one transaction.

    uv run --group dashboard python dashboard/db/load.py --bundle dashboard/db/bundle

Reads DATABASE_URL from the environment, or from the repo `.env` when the environment does not set it.
The URL is never printed. `schema.sql` drops and recreates every dashboard table, the CSVs are streamed in
with COPY, then the indexes are built. Everything runs in a single transaction, so a failed load leaves the
previous data untouched and running it again is safe.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import psycopg

REPO = Path(__file__).resolve().parents[2]
INDEX_MARKER = "-- @indexes"


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    env_file = REPO / ".env"
    if not url and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip().removeprefix("export ").strip() == "DATABASE_URL":
                url = value.strip().strip("'\"")
    if not url:
        sys.exit("DATABASE_URL is not set (environment or repo .env).")
    return url


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Load a dashboard bundle into Postgres.")
    ap.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent / "bundle")
    args = ap.parse_args(argv)

    schema = (args.bundle / "schema.sql").read_text(encoding="utf-8")
    tables_sql, _, index_sql = schema.partition(INDEX_MARKER)
    tables = re.findall(r"^CREATE TABLE (\w+)", tables_sql, flags=re.M)

    with psycopg.connect(database_url()) as conn:  # the block commits on success, rolls back on error
        with conn.cursor() as cur:
            cur.execute(tables_sql)
            for table in tables:
                path = args.bundle / f"{table}.csv"
                with open(path, encoding="utf-8") as fh:
                    header = fh.readline().strip()
                    columns = ", ".join(c.strip('"') for c in header.split(","))
                    with cur.copy(f"COPY {table} ({columns}) FROM STDIN WITH (FORMAT csv)") as copy:
                        while chunk := fh.read(1 << 20):
                            copy.write(chunk)
                cur.execute(f"SELECT count(*) FROM {table}")
                print(f"  {table:<15} {cur.fetchone()[0]:>9,} rows")
            if index_sql.strip():
                cur.execute(index_sql)
    print("loaded and committed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
