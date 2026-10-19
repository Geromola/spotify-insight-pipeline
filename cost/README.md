# Cost and runtime calculator

Measured pilot: `data/cost_100.csv` (SHA-256 `c884ac3b…dd0e`), run `pilot-100-s2d` on 18 Oct 2026 with
Claude Haiku 5.5 (enrich, fallback, group, diagnose, memo, critic) and Jev (verify and the diagnosis check), one
worker, empty result cache, taxonomy `t2`, schema `s2`, symptom catalog `y1`, label config
`anthropic:claude-haiku-5-5|disabled/low>adaptive/low|prompt=61e388fc59|tax=t2|schema=s2`.

| Run | Wall-clock | Model calls | API cost |
|---|---:|---:|---:|
| cold | 93.1 s | 41 | $0.016954 |
| warm (same settings, saved results) | 0.04 s | 0 | $0 |

The diagnose projection scales a single 13-complaint request, so it overstates the full-run cost: $5.00 projected
against $3.40 measured, because a 50-complaint request spreads its prompt and thinking over more complaints.

Earlier pilots are in `archive/`: `pilot-100-t0` (placeholder taxonomy), `pilot-100-t1`, `pilot-100-t2` (the
first full run's settings), `pilot-100-s2` (before the diagnosis stage) and `pilot-100-s2a`, the first `s2` pilot, which quarantined 10 of 100 reviews and
exposed the output-format bug described in the main README (model selection).

Files: `pilot_runs.json` (counts, timings, settings), `pilot_calls.jsonl` (every attempt with usage),
`usage.csv` (billed units per item), `pilot_records.jsonl` (the 100 records), `rates.csv` (dated prices),
`report.md` (the calculator's output).

## Offline replay (default, no API key, no model call)

```sh
python3.12 cost/calculator.py                        # recompute from usage.csv × rates.csv
python3.12 cost/calculator.py --write                # also save report.md
python3.12 cost/calculator.py --rate-multiplier 2    # API subtotal doubles; time and local cost do not
python3.12 cost/calculator.py --unique-texts 300000  # projection changes; measured rows do not
python3.12 cost/calculator.py --help                 # every editable assumption and control
```

Each billing item is `billed_units × price_per_unit`. Uncached input, cache writes, cache reads and output
are mutually exclusive; thinking tokens are already inside output and are not added again.

## Paid pilot (explicit)

```sh
.venv/bin/python cost/calculator.py pilot --paid --config config/haiku.toml --run <new-run-id>
```

Runs the whole chain on `data/cost_100.csv` cold, then warm, and writes the files above here. Importing or
opening the calculator starts nothing. Without `--config` it uses the offline fake provider and writes to
`runs/`, never here; that output is labelled synthetic.
