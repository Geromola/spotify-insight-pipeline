# 100-review cost and runtime calculator

Input `cost_100.csv` · sha256 `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e`

Label config: `anthropic:claude-haiku-5-5|disabled/low>adaptive/low|prompt=61e388fc59|tax=t2|schema=s2`

Rate multiplier applied: ×1 (rates come from `rates.csv`, with dated source links)

## Measured on the pilot

- Records: 100 · completed 100 · failed/quarantined 0
- Unique texts: 100 · result-cache hits: 0 · fallback texts: 2
- Workers: 1 · batch size limit: 50 · mode: sync
- Stages included: prepare, enrich, fallback, verify, finalize, group, diagnose, memo · not yet included: none

| Run | Stage | Model · tier | Requests | Texts | Uncached input | Cache writes | Cache reads | Output | Cost | Usage missing |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| cold | enrich·fallback | anthropic/claude-haiku-5-5 · standard | 2 | 2 | 111 | 3070 | 3070 | 561 | $0.000706 | 0 |
| cold | enrich·primary | anthropic/claude-haiku-5-5 · standard | 2 | 100 | 3515 | 3276 | 3276 | 2131 | $0.001859 | 0 |
| cold | group·diagnose | anthropic/claude-haiku-5-5 · standard | 1 | 13 | 695 | 2794 | 0 | 1944 | $0.001391 | 0 |
| cold | group·name | anthropic/claude-haiku-5-5 · standard | 1 | 40 | 3848 | 668 | 0 | 3333 | $0.002135 | 0 |
| cold | memo·critic | anthropic/claude-haiku-5-5 · standard | 1 | 27 | 15138 | 0 | 2002 | 3504 | $0.003286 | 0 |
| cold | memo·draft | anthropic/claude-haiku-5-5 · standard | 1 | 27 | 13001 | 0 | 3922 | 7400 | $0.005039 | 0 |
| cold | verify·diagnose | typesafe/jev-1.13.0 · standard | 13 | 13 | 11979 | 0 | 0 | 2507 | $0.000503 | 0 |
| cold | verify·verify | typesafe/jev-1.13.0 · standard | 20 | 20 | 48455 | 0 | 0 | 13884 | $0.002035 | 0 |

| Run | Wall-clock s | Model calls | API cost | Cost per 1,000 input rows | Cost per completed record | Rows per second |
|---|---:|---:|---:|---:|---:|---:|
| cold | 93.0756 | 41 | $0.016954 | $0.169542 | $0.000170 | 1.1 |
| warm | 0.0379 | 0 | $0.000000 | $0.000000 | $0.000000 | 2638.5 |

Warm run: **0 new enrichment calls** under unchanged settings.

Stage seconds (cold): diagnose 13.2067, enrich 9.6196, fallback 3.8681, finalize 0.0117, group 14.3842, memo 48.442, prepare 0.0028, verify 3.5404

Local compute: $0 assumed (a laptop running SQLite and Python); electricity and hardware are not measured.

## Estimated before the full run

Assumptions (all editable flags): rows 660,622; nonempty 660,609; empty-text quarantines 13; unique texts 484,189; verification sample 1,000; output-token cap 1400; max workers 1; max fallback fraction 2%; budget $15.00.

Model: the measured instruction prefix is paid once per request of up to 50 texts (a cache read once warm, ordinary input when uncached); each text's own input tokens are scaled by the ratio of mean text length (full corpus ÷ pilot); output is a fixed set of short fields per text.

| Scenario | Texts | Tier | Retries | Fallback | Enrich | Fallback cost | Fixed overhead | Diagnose | API total | Sync hours (modeled) | Budget |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| base: exact-text reuse | 484,189 | batch (ESTIMATED: tier not measured) | 1% | 2% | $3.82 | $1.71 | $0.112215 | $5.00 | $10.64 | 13.07 | within |
| no reuse: every nonempty row | 660,609 | batch (ESTIMATED: tier not measured) | 1% | 2% | $5.21 | $2.33 | $0.112215 | $5.00 | $12.65 | 17.83 | within |
| conservative: standard tier, 5% retries, max fallback, no prompt cache | 484,189 | standard | 5% | 2% | $10.94 | $3.42 | $0.112215 | $9.98 | $24.45 | 13.58 | **OVER BUDGET** |

Fixed overhead: verifier: measured cost per text x 1,000 declared sample; group and memo: measured once on the pilot, added once.

Diagnose stage (base scenario): 93,226 diagnosable texts x $0.000053 per diagnosed text (pilot: 13 diagnosable of 100 unique) + blind check $0.011610.

Sync hours are modeled from the pilot's one-worker throughput divided by the worker count; more workers shorten elapsed time but do not lower cost. Batch API turnaround (up to 24 h) is not modeled.
