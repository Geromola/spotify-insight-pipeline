# Diagnosis probe

200 development complaints (50 per area, seeded) from `full-660k-s2`'s diagnosable texts. Agreement is with Jev's blind symptom choice; Jev is a second opinion, not ground truth.

| Arm | Valid lines | Symptom 1 chosen | Other (0) | Agreement with Jev | access | usability | playback | billing/support | Output tokens per review | Batch cost per 1,000 reviews |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A | 200 of 200 | 28% | 28% | **56%** | 60% | 64% | 42% | 58% | 12.0 | $0.006 |
| B | 178 of 200 | 28% | 16% | **68%** | 61% | 76% | 65% | 70% | 13.3 | $0.007 |
| C | 200 of 200 | 24% | 26% | **75%** | 70% | 78% | 78% | 74% | 88.3 | $0.026 |
| D | 200 of 200 | 29% | 26% | **68%** | 72% | 62% | 64% | 76% | 72.5 | $0.022 |

- A vs B: same symptom on 63% of 178

- A vs C: same symptom on 57% of 200

- B vs C: same symptom on 73% of 178
