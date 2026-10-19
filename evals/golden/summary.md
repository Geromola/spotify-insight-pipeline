Golden 50 (hand-labelled by Gero, then aligned with the contract; 12 ambiguous cases, alternatives accepted). Sentiment tolerance declared before scoring: ±0.3.

| Metric | Haiku 5.5 · contract labels | Jev · contract labels | Haiku 5.5 · original labels | Jev · original labels |
|---|---:|---:|---:|---:|
| Topic agreement | 94% | 92% | 72% | 68% |
| Intent agreement | 92% | 90% | 82% | 82% |
| Severity exact agreement | 90% | 86% | 84% | 78% |
| All three right | 80% | 74% | 50% | 46% |
| Severity MAE | 0.14 | 0.18 | 0.24 | 0.28 |
| Sentiment MAE | 0.23 | 0.18 | 0.27 | 0.19 |
| Sentiment within ±0.3 | 64% | 72% | 58% | 70% |
| Quote is an exact substring | 100% | 100% | 100% | 100% |
| Quote overlaps the human quote | 90% | 94% | 90% | 94% |
| Free-tier limit agreement (n=50) | 94% | 88% | — | — |
| Stated plan agreement (n=50) | 88% | 86% | — | — |

Valid predictions: Haiku 50/50, Jev 50/50. needs_review vs human flag (Haiku): {'tp': 8, 'fp': 2, 'fn': 8, 'tn': 32}.

Topic confusions (human -> model), Haiku: downloads -> billing ×1, other -> usability ×1, support -> playback ×1, usability -> billing ×1, usability -> playback ×1
Intent confusions (human -> model), Haiku: cancellation -> complaint ×1, complaint -> request ×1, praise -> complaint ×1, unclear -> praise ×1
Topic confusions, Jev: access -> playback ×1, catalog -> usability ×1, other -> billing ×1, support -> playback ×1, usability -> billing ×1, usability -> playback ×1
Intent confusions, Jev: complaint -> request ×1, praise -> complaint ×1, praise -> unclear ×2, request -> complaint ×1, unclear -> praise ×1
