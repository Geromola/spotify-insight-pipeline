# 100-review cost and runtime calculator

Input `cost_100.csv` · sha256 `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e`

Label config: `anthropic:claude-haiku-5-5|disabled/low>adaptive/low|prompt=633ed0cccb|tax=t2|schema=s1`

Rate multiplier applied: ×1 (rates come from `rates.csv`, with dated source links)

## Measured on the pilot

- Records: 100 · completed 100 · failed/quarantined 0
- Unique texts: 100 · result-cache hits: 0 · fallback texts: 2
- Workers: 1 · batch size limit: 50 · mode: sync
- Stages included: prepare, enrich, fallback, verify, finalize, group, memo · not yet included: none

| Run | Stage | Model · tier | Requests | Texts | Uncached input | Cache writes | Cache reads | Output | Cost | Usage missing |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| cold | enrich·fallback | anthropic/claude-haiku-5-5 · standard | 2 | 2 | 58 | 2733 | 2733 | 466 | $0.000608 | 0 |
| cold | enrich·primary | anthropic/claude-haiku-5-5 · standard | 2 | 100 | 3515 | 2884 | 2884 | 1736 | $0.001609 | 0 |
| cold | group·name | anthropic/claude-haiku-5-5 · standard | 1 | 40 | 3643 | 668 | 0 | 3179 | $0.002037 | 0 |
| cold | memo·critic | anthropic/claude-haiku-5-5 · standard | 2 | 40 | 13179 | 678 | 678 | 3581 | $0.003200 | 0 |
| cold | memo·draft | anthropic/claude-haiku-5-5 · standard | 1 | 20 | 4720 | 1232 | 0 | 4079 | $0.002666 | 0 |
| cold | memo·revision | anthropic/claude-haiku-5-5 · standard | 1 | 20 | 6862 | 0 | 1232 | 4158 | $0.002778 | 0 |
| cold | verify·verify | typesafe/jev-1.13.0 · standard | 20 | 20 | 43895 | 0 | 0 | 12581 | $0.001844 | 0 |

| Run | Wall-clock s | Model calls | API cost | Cost per 1,000 input rows | Cost per completed record | Rows per second |
|---|---:|---:|---:|---:|---:|---:|
| cold | 76.5064 | 29 | $0.014740 | $0.147404 | $0.000147 | 1.3 |
| warm | 0.0309 | 0 | $0.000000 | $0.000000 | $0.000000 | 3236.2 |

Warm run: **0 new enrichment calls** under unchanged settings.

Stage seconds (cold): enrich 8.6165, fallback 3.6755, finalize 0.0113, group 12.3307, memo 48.6172, prepare 0.0036, verify 3.2514

Local compute: $0 assumed (a laptop running SQLite and Python); electricity and hardware are not measured.

## Estimated before the full run

Assumptions (all editable flags): rows 660,622; nonempty 660,609; empty-text quarantines 13; unique texts 484,189; verification sample 1,000; output-token cap 1200; max workers 1; max fallback fraction 2%; budget $15.00.

Model: the measured instruction prefix is paid once per request of up to 50 texts (a cache read once warm, ordinary input when uncached); each text's own input tokens are scaled by the ratio of mean text length (full corpus ÷ pilot); output is a fixed set of short fields per text.

| Scenario | Texts | Tier | Retries | Fallback | Enrich | Fallback cost | Fixed overhead | API total | Sync hours (modeled) | Budget |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| base: exact-text reuse | 484,189 | batch (ESTIMATED: tier not measured) | 1% | 2% | $3.32 | $1.47 | $0.102860 | $4.89 | 11.70 | within |
| no reuse: every nonempty row | 660,609 | batch (ESTIMATED: tier not measured) | 1% | 2% | $4.53 | $2.01 | $0.102860 | $6.64 | 15.97 | within |
| conservative: standard tier, 5% retries, max fallback, no prompt cache | 484,189 | standard | 5% | 2% | $9.54 | $2.94 | $0.102860 | $12.58 | 12.17 | within |

Fixed overhead: verifier: measured cost per text x 1,000 declared sample; group and memo: measured once on the pilot, added once.

Sync hours are modeled from the pilot's one-worker throughput divided by the worker count; more workers shorten elapsed time but do not lower cost. Batch API turnaround (up to 24 h) is not modeled.
