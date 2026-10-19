# 100-review cost and runtime calculator

Input `cost_100.csv` · sha256 `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e`

Label config: `anthropic:claude-haiku-5-5|disabled/low>adaptive/low|prompt=e58899a6cb|tax=t0|schema=s1`

Rate multiplier applied: ×1 (rates come from `rates.csv`, with dated source links)

## Measured on the pilot

- Records: 100 · completed 100 · failed/quarantined 0
- Unique texts: 100 · result-cache hits: 0 · fallback texts: 2
- Workers: 1 · batch size limit: 50 · mode: sync
- Stages included: prepare, enrich, fallback, verify, finalize, group, memo · not yet included: none

| Run | Stage | Model · tier | Requests | Texts | Uncached input | Cache writes | Cache reads | Output | Cost | Usage missing |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| cold | enrich·fallback | anthropic/claude-haiku-5-5 · standard | 2 | 2 | 164 | 1548 | 1548 | 526 | $0.000488 | 0 |
| cold | enrich·primary | anthropic/claude-haiku-5-5 · standard | 2 | 100 | 3515 | 1699 | 1699 | 1731 | $0.001446 | 0 |
| cold | group·name | anthropic/claude-haiku-5-5 · standard | 1 | 25 | 2225 | 668 | 0 | 2145 | $0.001378 | 0 |
| cold | memo·critic | anthropic/claude-haiku-5-5 · standard | 2 | 34 | 11482 | 669 | 669 | 3363 | $0.002920 | 0 |
| cold | memo·draft | anthropic/claude-haiku-5-5 · standard | 1 | 17 | 3833 | 1209 | 0 | 3798 | $0.002433 | 0 |
| cold | memo·revision | anthropic/claude-haiku-5-5 · standard | 1 | 17 | 6016 | 0 | 1209 | 3460 | $0.002344 | 0 |
| cold | verify·verify | typesafe/jev-1.13.0 · standard | 20 | 20 | 18375 | 0 | 0 | 4092 | $0.000772 | 0 |

| Run | Wall-clock s | Model calls | API cost | Cost per 1,000 input rows | Cost per completed record | Rows per second |
|---|---:|---:|---:|---:|---:|---:|
| cold | 77.9151 | 29 | $0.011782 | $0.117821 | $0.000118 | 1.3 |
| warm | 0.032 | 0 | $0.000000 | $0.000000 | $0.000000 | 3125.0 |

Warm run: **0 new enrichment calls** under unchanged settings.

Stage seconds (cold): enrich 11.1166, fallback 4.3332, finalize 0.0115, group 10.151, memo 49.0211, prepare 0.0063, verify 3.2753

Local compute: $0 assumed (a laptop running SQLite and Python); electricity and hardware are not measured.

## Estimated before the full run

Assumptions (all editable flags): rows 660,622; nonempty 660,609; empty-text quarantines 13; unique texts 484,189; verification sample 1,000; output-token cap 1200; max workers 1; max fallback fraction 2%; budget $15.00.

Model: the measured instruction prefix is paid once per request of up to 50 texts (a cache read once warm, ordinary input when uncached); each text's own input tokens are scaled by the ratio of mean text length (full corpus ÷ pilot); output is a fixed set of short fields per text.

| Scenario | Texts | Tier | Retries | Fallback | Enrich | Fallback cost | Fixed overhead | API total | Sync hours (modeled) | Budget |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| base: exact-text reuse | 484,189 | batch (ESTIMATED: tier not measured) | 1% | 2% | $3.25 | $1.18 | $0.047663 | $4.48 | 15.10 | within |
| no reuse: every nonempty row | 660,609 | batch (ESTIMATED: tier not measured) | 1% | 2% | $4.44 | $1.61 | $0.047663 | $6.10 | 20.60 | within |
| conservative: standard tier, 5% retries, max fallback, no prompt cache | 484,189 | standard | 5% | 2% | $8.32 | $2.36 | $0.047663 | $10.73 | 15.70 | within |

Fixed overhead: verifier: measured cost per text x 1,000 declared sample; group and memo: measured once on the pilot, added once.

Sync hours are modeled from the pilot's one-worker throughput divided by the worker count; more workers shorten elapsed time but do not lower cost. Batch API turnaround (up to 24 h) is not modeled.
