#!/bin/bash
# Waits until at least one provider batch has been collected (saved work) while another is still
# running (work in flight), then sends SIGINT to the enrichment process, exactly what Ctrl+C sends.
cd "$(dirname "$0")/../.."
DB=runs/full-660k-t2/pipeline.sqlite
until [ "$(sqlite3 $DB "SELECT (SELECT count(*) FROM provider_batches WHERE status='completed') >= 1
                         AND (SELECT count(*) FROM provider_batches WHERE status='in_progress') >= 1" 2>/dev/null)" = "1" ]; do
  sleep 10
done
PID=$(pgrep -f -- '-m pipeline enrich --run full-660k-t2')  # the Python process itself, not the bash wrapper
echo "$(date -u +%FT%TZ) saved work exists and a batch is in flight; sending SIGINT to pid $PID"
sqlite3 -header -column $DB "SELECT status, count(*) n FROM texts GROUP BY status; SELECT provider_batch, status, n_requests FROM provider_batches;"
[ -n "$PID" ] || { echo "enrichment process not found"; exit 1; }
kill -INT $PID
