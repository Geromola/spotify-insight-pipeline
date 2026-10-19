#!/bin/bash
# Interruption/resume demo, part 1: start the full-corpus enrichment through the Batch API.
# A watcher (interrupt_when_ready.sh) sends SIGINT, the signal Ctrl+C sends, once saved work exists.
cd "$(dirname "$0")/../.."
RUN=full-660k-s2
echo "== $(date -u +%FT%TZ) part 1: backlog before enrichment =="
.venv/bin/python -m pipeline status --run $RUN
echo "== $(date -u +%FT%TZ) start enrichment =="
.venv/bin/python -m pipeline enrich --run $RUN --config config/haiku.toml --mode batch --poll-seconds 30
code=$?  # read before anything else resets it
echo "== $(date -u +%FT%TZ) enrichment exited with code $code =="
.venv/bin/python -m pipeline status --run $RUN
