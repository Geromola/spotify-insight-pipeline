# 100-review cost and runtime calculator

Input `cost_100.csv` · sha256 `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e`

Label config: `anthropic:claude-haiku-5-5|disabled/low>adaptive/low|prompt=360c97db74|tax=t1|schema=s1`

Rate multiplier applied: ×1 (rates come from `rates.csv`, with dated source links)

## Measured on the pilot

- Records: 100 · completed 100 · failed/quarantined 0
- Unique texts: 100 · result-cache hits: 0 · fallback texts: 2
- Workers: 1 · batch size limit: 50 · mode: sync
- Stages included: prepare, enrich, fallback, verify, finalize, group, memo · not yet included: none

| Run | Stage | Model · tier | Requests | Texts | Uncached input | Cache writes | Cache reads | Output | Cost | Usage missing |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| cold | enrich·fallback | anthropic/claude-haiku-5-5 · standard | 2 | 2 | 239 | 2734 | 2734 | 734 | $0.000760 | 0 |
| cold | enrich·primary | anthropic/claude-haiku-5-5 · standard | 2 | 100 | 3515 | 2885 | 2885 | 1734 | $0.001608 | 0 |
| cold | group·name | anthropic/claude-haiku-5-5 · standard | 1 | 41 | 3812 | 668 | 0 | 3654 | $0.002292 | 0 |
| cold | memo·critic | anthropic/claude-haiku-5-5 · standard | 2 | 38 | 12589 | 678 | 678 | 5091 | $0.003896 | 0 |
| cold | memo·draft | anthropic/claude-haiku-5-5 · standard | 1 | 19 | 4807 | 1232 | 0 | 3997 | $0.002633 | 0 |
| cold | memo·revision | anthropic/claude-haiku-5-5 · standard | 1 | 19 | 6591 | 0 | 1232 | 4067 | $0.002705 | 0 |
| cold | verify·verify | typesafe/jev-1.13.0 · standard | 20 | 20 | 43875 | 0 | 0 | 12573 | $0.001843 | 0 |

| Run | Wall-clock s | Model calls | API cost | Cost per 1,000 input rows | Cost per completed record | Rows per second |
|---|---:|---:|---:|---:|---:|---:|
| cold | 87.809 | 29 | $0.015736 | $0.157365 | $0.000157 | 1.1 |
| warm | 0.0308 | 0 | $0.000000 | $0.000000 | $0.000000 | 3246.8 |

Warm run: **0 new enrichment calls** under unchanged settings.

Stage seconds (cold): enrich 8.6326, fallback 5.5403, finalize 0.0113, group 14.3432, memo 55.1431, prepare 0.0029, verify 4.1355

Local compute: $0 assumed (a laptop running SQLite and Python); electricity and hardware are not measured.

## Estimated before the full run

Assumptions (all editable flags): rows 660,622; nonempty 660,609; empty-text quarantines 13; unique texts 484,189; verification sample 1,000; output-token cap 1200; max workers 1; max fallback fraction 2%; budget $15.00.

Model: the measured instruction prefix is paid once per request of up to 50 texts (a cache read once warm, ordinary input when uncached); each text's own input tokens are scaled by the ratio of mean text length (full corpus ÷ pilot); output is a fixed set of short fields per text.

| Scenario | Texts | Tier | Retries | Fallback | Enrich | Fallback cost | Fixed overhead | API total | Sync hours (modeled) | Budget |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| base: exact-text reuse | 484,189 | batch (ESTIMATED: tier not measured) | 1% | 2% | $3.31 | $1.84 | $0.103663 | $5.26 | 11.73 | within |
| no reuse: every nonempty row | 660,609 | batch (ESTIMATED: tier not measured) | 1% | 2% | $4.52 | $2.51 | $0.103663 | $7.14 | 16.00 | within |
| conservative: standard tier, 5% retries, max fallback, no prompt cache | 484,189 | standard | 5% | 2% | $9.53 | $3.68 | $0.103663 | $13.31 | 12.19 | within |

Fixed overhead: verifier: measured cost per text x 1,000 declared sample; group and memo: measured once on the pilot, added once.

Sync hours are modeled from the pilot's one-worker throughput divided by the worker count; more workers shorten elapsed time but do not lower cost. Batch API turnaround (up to 24 h) is not modeled.
