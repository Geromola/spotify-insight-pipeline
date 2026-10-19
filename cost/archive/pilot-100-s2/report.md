# 100-review cost and runtime calculator

Input `cost_100.csv` · sha256 `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e`

Label config: `anthropic:claude-haiku-5-5|disabled/low>adaptive/low|prompt=61e388fc59|tax=t2|schema=s2`

Rate multiplier applied: ×1 (rates come from `rates.csv`, with dated source links)

## Measured on the pilot

- Records: 100 · completed 100 · failed/quarantined 0
- Unique texts: 100 · result-cache hits: 0 · fallback texts: 2
- Workers: 1 · batch size limit: 50 · mode: sync
- Stages included: prepare, enrich, fallback, verify, finalize, group, memo · not yet included: none

| Run | Stage | Model · tier | Requests | Texts | Uncached input | Cache writes | Cache reads | Output | Cost | Usage missing |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| cold | enrich·fallback | anthropic/claude-haiku-5-5 · standard | 2 | 2 | 65 | 3070 | 3070 | 692 | $0.000767 | 0 |
| cold | enrich·primary | anthropic/claude-haiku-5-5 · standard | 2 | 100 | 3515 | 0 | 6552 | 2136 | $0.001485 | 0 |
| cold | group·name | anthropic/claude-haiku-5-5 · standard | 1 | 42 | 4021 | 668 | 0 | 2072 | $0.001522 | 0 |
| cold | memo·critic | anthropic/claude-haiku-5-5 · standard | 2 | 48 | 17485 | 815 | 815 | 7116 | $0.005417 | 0 |
| cold | memo·draft | anthropic/claude-haiku-5-5 · standard | 1 | 24 | 7125 | 1688 | 0 | 5354 | $0.003600 | 0 |
| cold | memo·revision | anthropic/claude-haiku-5-5 · standard | 1 | 24 | 9123 | 0 | 1688 | 5393 | $0.003626 | 0 |
| cold | verify·verify | typesafe/jev-1.13.0 · standard | 20 | 20 | 48455 | 0 | 0 | 13879 | $0.002035 | 0 |

| Run | Wall-clock s | Model calls | API cost | Cost per 1,000 input rows | Cost per completed record | Rows per second |
|---|---:|---:|---:|---:|---:|---:|
| cold | 97.9098 | 29 | $0.018451 | $0.184514 | $0.000185 | 1.0 |
| warm | 0.0351 | 0 | $0.000000 | $0.000000 | $0.000000 | 2849.0 |

Warm run: **0 new enrichment calls** under unchanged settings.

Stage seconds (cold): enrich 9.9678, fallback 4.5033, finalize 0.012, group 8.9396, memo 71.4176, prepare 0.0027, verify 3.0668

Local compute: $0 assumed (a laptop running SQLite and Python); electricity and hardware are not measured.

## Estimated before the full run

Assumptions (all editable flags): rows 660,622; nonempty 660,609; empty-text quarantines 13; unique texts 484,189; verification sample 1,000; output-token cap 1400; max workers 1; max fallback fraction 2%; budget $15.00.

Model: the measured instruction prefix is paid once per request of up to 50 texts (a cache read once warm, ordinary input when uncached); each text's own input tokens are scaled by the ratio of mean text length (full corpus ÷ pilot); output is a fixed set of short fields per text.

| Scenario | Texts | Tier | Retries | Fallback | Enrich | Fallback cost | Fixed overhead | API total | Sync hours (modeled) | Budget |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| base: exact-text reuse | 484,189 | batch (ESTIMATED: tier not measured) | 1% | 2% | $3.83 | $1.86 | $0.115920 | $5.80 | 13.54 | within |
| no reuse: every nonempty row | 660,609 | batch (ESTIMATED: tier not measured) | 1% | 2% | $5.22 | $2.53 | $0.115920 | $7.87 | 18.47 | within |
| conservative: standard tier, 5% retries, max fallback, no prompt cache | 484,189 | standard | 5% | 2% | $10.95 | $3.71 | $0.115920 | $14.78 | 14.08 | within |

Fixed overhead: verifier: measured cost per text x 1,000 declared sample; group and memo: measured once on the pilot, added once.

Sync hours are modeled from the pilot's one-worker throughput divided by the worker count; more workers shorten elapsed time but do not lower cost. Batch API turnaround (up to 24 h) is not modeled.
