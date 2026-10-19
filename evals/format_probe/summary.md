# Output-format probe (s2 enricher line)

Development rows 100-499 of `checkpoint_500.csv`, packed by length into requests of 50, each layout sent 2 times. Claude Haiku 5.5, thinking off, standard tier.

| Layout | Requests | Requests with a field-count failure | Lines failing | Lines valid | Output tokens per review | Cost (standard) |
|---|---:|---:|---:|---:|---:|---:|
| A | 16 | 2 | 79 of 800 | 721 of 800 | 21.1 (worst request 21.6) | $0.0119 |
| B | 16 | 0 | 0 of 800 | 800 of 800 | 21.3 (worst request 21.6) | $0.0120 |
| C | 16 | 0 | 0 of 800 | 799 of 800 | 21.3 (worst request 21.6) | $0.0120 |

Total spent: $0.0359.

Label agreement with layout A on the reviews both parsed (first repeat):

- B vs A on 350 reviews: topic 0.906, free-tier limit 0.954, plan 0.971
- C vs A on 349 reviews: topic 0.905, free-tier limit 0.943, plan 0.971

Free-tier limit and plan counts (first repeat):

- A: 54 free-tier limit of 350; plan {'unstated': 328, 'free': 9, 'paid': 13}
- B: 64 free-tier limit of 400; plan {'unstated': 378, 'free': 9, 'paid': 13}
- C: 66 free-tier limit of 399; plan {'unstated': 380, 'free': 7, 'paid': 12}
