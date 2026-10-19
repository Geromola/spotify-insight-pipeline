# Focus next quarter on usability, the largest in-scope product area, starting with ad interruptions and the limited skip and replay controls.

**Recommended priority: usability**

**Recommendation.** Put next quarter's effort into usability. It has the largest product-area totals: 90062 complaints and severity sum 206855. Start with `usability.ads-interruptions` (Frequent ads interrupt listening), with 36883 complaints [C1], severity sum 76209 [C2] and priority score 76209 [C3]. Then take `usability.shuffle-playback-control` (Skip and replay limits frustrate listeners), with 20684 complaints [C4], severity sum 55711 [C5] and priority score 55711 [C6]. Reviews b8533570-ea2e-4d06-b841-a83dda5ea8cc and 502db020-81c2-4102-8e8b-c72063d7b2c1 describe ads that are too frequent and irrelevant. Reviews 40e7479a-4547-4936-ba92-c2711aa7037e, 4df8b195-0efe-4f85-920c-0995442f5da7 and 22712ace-11d1-4343-a389-ba04564b158f describe being unable to skip, loop, replay or rewind. **Why.** Usability has the most complaints and severity of the four areas in scope, and its two largest issues are both about ads and in-app control. **Alternatives.** Billing/support has 49467 complaints and severity sum 126424. Its top issue, `billing.premium-paywall-features` (Core features moved behind premium paywall), has priority score 89058 [C7], the largest single in-scope score. It is a pricing and tier question, though, and it overlaps only partly with the premium-gated controls above. Playback has 33289 complaints and severity sum 101152. Its most specific issue, `playback.playback-stops-crashes`, has priority score 35317 [C8]. Access is smallest, with 11654 complaints and severity sum 44499. Its top issue, `access.general`, has priority score 15383 [C9]. **Evidence.** Issue numbers come from the code-computed ranking; area totals come from the product-area summary. Outside the question are downloads, catalog and other, with 111543 complaints. **Limitations.** Reviews are self-selected public reviews from one window, 2022-05-17 to 2023-11-15. There is no revenue, plan or churn data, so cancellation language is expressed intent, not observed churn. 31 rows were quarantined or unclassified, and 67504 texts need review. On a 1000-review check, verifier agreement was 0.816 for topic, 0.893 for intent and 0.762 for severity, with severity MAE 0.278.

## Claims (checked by code against `ranking.csv`)

| Claim | Issue | Metric | Value |
|---|---|---|---:|
| C1 | `usability.ads-interruptions` | complaint_count | 36883 |
| C2 | `usability.ads-interruptions` | severity_sum | 76209 |
| C3 | `usability.ads-interruptions` | priority_score | 76209 |
| C4 | `usability.shuffle-playback-control` | complaint_count | 20684 |
| C5 | `usability.shuffle-playback-control` | severity_sum | 55711 |
| C6 | `usability.shuffle-playback-control` | priority_score | 55711 |
| C7 | `billing.premium-paywall-features` | priority_score | 89058 |
| C8 | `playback.playback-stops-crashes` | priority_score | 35317 |
| C9 | `access.general` | priority_score | 15383 |

## Review trail

- Critic on the first draft: **revise** (Cite C2 (usability.ads-interruptions severity_sum 76209) with its claim ID in the memo, or remove it from claims, since code validation found it listed but never cited.; Cite C5 (usability.shuffle-playback-control severity_sum 55711) with its claim ID in the memo, or remove it from claims, since code validation found it listed but never cited.; Correct the claim that playback.playback-stops-crashes has the highest mean severity among playback issues: playback.offline-false-errors has mean severity 3.389716, which is higher than 3.291426.; Correct the claim that playback.playback-stops-crashes is the top playback issue: playback.general has priority score 45961, higher than its 35317, so state the top playback issue by priority score or rephrase.; Verify the word count is at most about 350 words by counting it exactly, and trim if needed for a busy executive.)
- Revised: yes, once
- Critic on the final memo: **revise**
- Unresolved code-check problems: none
