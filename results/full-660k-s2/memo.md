# Playback should get next quarter's effort: music stops, will not start, or the app crashes while people listen, and playback leads on concrete defects in both windows.

**Recommended priority: playback**

## Recommendation
Music stops or will not start, and the app crashes while people listen.

Recommend playback, driven by the concrete-defect lens. Vague `other.general` leads the baseline [C1] but names no fix, so it cannot direct the choice; `playback.playback-stops-crashes` scores [C2] in the baseline.

## What to fix first
The four most severe concrete playback symptoms, in order; audio glitches ranks lower on severity.
- **App crashes or freezes**: 6,052 complaints (2,103 in the first six full months, 1,769 in the last six), 721 from paying reviewers; 13.7% follow an update; 7.0% of reviews on version 8.7.34.1312 report it, against 1.1% across all versions. "The app is frequently experiencing hangs, there's been an increase in ads, and the quality of songs has also decreased." Next step: Reproduce crashes and freezes on 8.7.34.1312 during active use and after updates, then fix the failing path.
- **Music pauses or stops on its own**: 6,005 complaints (1,890 in the first six full months, 1,845 in the last six), 874 from paying reviewers; 14.7% start in the background; 6.1% of reviews on version 8.7.90.544 report it, against 1.1% across all versions. "From the past week, the songs have been abruptly cut, and there's no problem in connection." Next step: Reproduce music halting with the app open in the background on 8.7.90.544, then fix the background playback stop.
- **Chosen music refuses to start playing**: 3,895 complaints (1,367 in the first six full months, 1,130 in the last six), 392 from paying reviewers; 14.6% follow an update; 3.3% of reviews on version 8.7.90.544 report it, against 0.7% across all versions. "For example, when I try to play my personal playlist, it won't play no matter what I do UNLESS I play" Next step: Reproduce silent play presses after an update on 8.7.90.544, then fix the start path for chosen tracks.
- **Shows no internet when connected**: 1,956 complaints (698 in the first six full months, 600 in the last six), 167 from paying reviewers; 1.0% of reviews on version 8.8.2.434 report it, against 0.3% across all versions. Next step: Reproduce the no-connection message on 8.8.2.434 while data works, then fix the connectivity check.

## Why
Playback leads usability on concrete defects in the full window (24,845 vs 15,204) and in the last six full months (7,577 vs 5,254), so recent months agree and this recommendation follows both. Usability has the most raw complaints, but 44.1% of its diagnosed complaints name no symptom.

## Free versus premium
Free-tier limits by design, including plan-gated controls such as song choice, are 62.6% of usability but 2.3% of playback. Without them, other.general still leads, then playback.general, and among paying reviewers playback.playback-stops-crashes ranks first. Paying reviewers report 2,885 concrete playback defects against 1,471 for usability.

## Alternatives
Access has 9,289 concrete defects and 4,984 in the last six months. Billing/support has 7,182 concrete defects, 3,323 in the last six months, and 1,748 among paying reviewers, the most after playback. Downloads and catalog sit outside the question.

## Limitations
Self-selected historical reviews from one window, 2022-05-17 to 2023-11-15. No revenue, plan or churn data: the plan is only what reviewers state, and cancellation language is expressed intent, not observed churn. 55 rows were quarantined or unclassified; 58,730 texts need review. Verifier agreement: topic 0.798, intent 0.893, severity 0.753, free-tier 0.928, plan 0.944. Symptom labels are model-assigned from an approved catalog; blind symptom agreement was 0.7933 (n=300), and 32.55% of diagnosed complaints are other.

## Claims (checked by code against `ranking.csv`)

| Claim | Issue | Metric | Value |
|---|---|---|---:|
| C1 | `other.general` | priority_score | 167110 |
| C2 | `playback.playback-stops-crashes` | priority_score | 35999 |

## Review trail

- Critic on the first draft: **revise** (In the fixes list, audio-glitches-or-drops (2,003 complaints) is larger than the fourth chosen symptom, false-no-internet-message (1,956 complaints), and is omitted without any stated reason. Either swap it in or add a sentence in the prose explaining why it is left out.; The Free versus premium section does not say what leads once free-tier limits are set aside. The lens ranking shows other.general still ranks first without free-tier limits, with playback.general second. State that explicitly rather than mentioning only playback.playback-stops-crashes in third place.; The Free versus premium section says 'Without them, paying reviewers report 2,885 concrete playback defects against 1,471 for usability', but those are concrete (plan-gated-excluded) paying counts, not a without-free-tier-limits figure. Reword so the basis matches the source, and say what paying reviewers complain about, e.g. that playback.playback-stops-crashes leads the paying-reviewers-only ranking.)
- Revised: yes, once
- Critic on the final memo: **pass**
- Unresolved code-check problems: none
