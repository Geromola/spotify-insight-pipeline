# 100-review cost and runtime calculator

Input `cost_100.csv` · sha256 `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e`

Label config: `anthropic:claude-haiku-5-5|disabled/low>adaptive/low|prompt=1d13902016|tax=t2|schema=s2`

Rate multiplier applied: ×1 (rates come from `rates.csv`, with dated source links)

## Measured on the pilot

- Records: 100 · completed 90 · failed/quarantined 10
- Unique texts: 100 · result-cache hits: 0 · fallback texts: 2
- Workers: 1 · batch size limit: 50 · mode: sync
- Stages included: prepare, enrich, fallback, verify, finalize, group, memo · not yet included: none

| Run | Stage | Model · tier | Requests | Texts | Uncached input | Cache writes | Cache reads | Output | Cost | Usage missing |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| cold | enrich·fallback | anthropic/claude-haiku-5-5 · standard | 2 | 2 | 190 | 3076 | 3076 | 860 | $0.000864 | 0 |
| cold | enrich·primary | anthropic/claude-haiku-5-5 · standard | 9 | 152 | 4399 | 3282 | 26256 | 3100 | $0.002663 | 0 |
| cold | group·name | anthropic/claude-haiku-5-5 · standard | 1 | 40 | 3730 | 668 | 0 | 1930 | $0.001422 | 0 |
| cold | memo·critic | anthropic/claude-haiku-5-5 · standard | 2 | 50 | 18392 | 815 | 815 | 4123 | $0.004011 | 0 |
| cold | memo·draft | anthropic/claude-haiku-5-5 · standard | 1 | 25 | 7331 | 1688 | 0 | 5585 | $0.003737 | 0 |
| cold | memo·revision | anthropic/claude-haiku-5-5 · standard | 1 | 25 | 9498 | 0 | 1688 | 4791 | $0.003362 | 0 |
| cold | verify·verify | typesafe/jev-1.13.0 · standard | 18 | 18 | 43655 | 0 | 0 | 12511 | $0.001834 | 0 |

| Run | Wall-clock s | Model calls | API cost | Cost per 1,000 input rows | Cost per completed record | Rows per second |
|---|---:|---:|---:|---:|---:|---:|
| cold | 99.6979 | 34 | $0.017891 | $0.178915 | $0.000199 | 1.0 |
| warm | 0.0341 | 0 | $0.000000 | $0.000000 | $0.000000 | 2932.6 |

Warm run: **0 new enrichment calls** under unchanged settings.

Stage seconds (cold): enrich 18.2818, fallback 5.1067, finalize 0.0121, group 8.2326, memo 63.0541, prepare 0.0029, verify 5.0078

Local compute: $0 assumed (a laptop running SQLite and Python); electricity and hardware are not measured.

## Estimated before the full run

Assumptions (all editable flags): rows 660,622; nonempty 660,609; empty-text quarantines 13; unique texts 484,189; verification sample 1,000; output-token cap 1400; max workers 1; max fallback fraction 2%; budget $15.00.

Model: the measured instruction prefix is paid once per request of up to 50 texts (a cache read once warm, ordinary input when uncached); each text's own input tokens are scaled by the ratio of mean text length (full corpus ÷ pilot); output is a fixed set of short fields per text.

| Scenario | Texts | Tier | Retries | Fallback | Enrich | Fallback cost | Fixed overhead | API total | Sync hours (modeled) | Budget |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| base: exact-text reuse | 484,189 | batch (ESTIMATED: tier not measured) | 1% | 2% | $3.52 | $2.09 | $0.114393 | $5.73 | 24.83 | within |
| no reuse: every nonempty row | 660,609 | batch (ESTIMATED: tier not measured) | 1% | 2% | $4.80 | $2.85 | $0.114393 | $7.77 | 33.88 | within |
| conservative: standard tier, 5% retries, max fallback, no prompt cache | 484,189 | standard | 5% | 2% | $10.32 | $4.18 | $0.114393 | $14.62 | 25.82 | within |

Fixed overhead: verifier: measured cost per text x 1,000 declared sample; group and memo: measured once on the pilot, added once.

Sync hours are modeled from the pilot's one-worker throughput divided by the worker count; more workers shorten elapsed time but do not lower cost. Batch API turnaround (up to 24 h) is not modeled.
