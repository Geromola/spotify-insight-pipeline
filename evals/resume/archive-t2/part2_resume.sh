#!/bin/bash
# Interruption/resume demo, part 2: the same command again. Completed texts are not sent again;
# batches already submitted before the interruption are polled, not re-sent.
cd "$(dirname "$0")/../.."
RUN=full-660k-t2
echo "== $(date -u +%FT%TZ) part 2: state after the interruption =="
.venv/bin/python -m pipeline status --run $RUN
python3 -c "import json; d=json.load(open('runs/$RUN/checkpoints/inv1.json')); print('checkpoint before:', d['completed_count'], 'completed review ids, ended:', d['ended'])"
echo "== $(date -u +%FT%TZ) resume enrichment =="
.venv/bin/python -m pipeline enrich --run $RUN --mode batch --poll-seconds 30
code=$?  # read before anything else resets it
echo "== $(date -u +%FT%TZ) enrichment exited with code $code =="
.venv/bin/python -m pipeline status --run $RUN
