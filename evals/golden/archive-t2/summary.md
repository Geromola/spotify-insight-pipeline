Golden 50 (hand-labelled by Gero, then aligned with the contract; 12 ambiguous cases, alternatives accepted). Sentiment tolerance declared before scoring: ±0.3.

| Metric | Haiku 5.5 · contract labels | Jev · contract labels | Haiku 5.5 · original labels | Jev · original labels |
|---|---:|---:|---:|---:|
| Topic agreement | 90% | 92% | 70% | 68% |
| Intent agreement | 92% | 86% | 86% | 78% |
| Severity exact agreement | 92% | 86% | 86% | 78% |
| All three right | 78% | 72% | 50% | 46% |
| Severity MAE | 0.12 | 0.18 | 0.20 | 0.30 |
| Sentiment MAE | 0.26 | 0.18 | 0.28 | 0.19 |
| Sentiment within ±0.3 | 60% | 76% | 58% | 74% |
| Quote is an exact substring | 100% | 100% | 100% | 100% |
| Quote overlaps the human quote | 90% | 92% | 90% | 92% |

Valid predictions: Haiku 50/50, Jev 50/50. needs_review vs human flag (Haiku): {'tp': 6, 'fp': 0, 'fn': 10, 'tn': 34}.

Topic confusions (human -> model), Haiku: access -> playback ×1, catalog -> other ×1, downloads -> billing ×1, other -> billing ×1, support -> playback ×1, usability -> billing ×1, usability -> other ×1, usability -> playback ×1
Intent confusions (human -> model), Haiku: cancellation -> complaint ×1, complaint -> request ×1, complaint -> unclear ×1, praise -> complaint ×1, praise -> unclear ×1, unclear -> praise ×1
Topic confusions, Jev: access -> playback ×1, catalog -> usability ×1, other -> billing ×1, support -> playback ×1, usability -> billing ×1, usability -> playback ×1
Intent confusions, Jev: complaint -> request ×1, praise -> complaint ×1, praise -> unclear ×2, request -> complaint ×1, unclear -> cancellation ×1, unclear -> complaint ×1, unclear -> praise ×1
