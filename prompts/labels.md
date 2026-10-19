TOPICS (code = meaning)
ac = access: login, signup, password, account access
us = usability: navigation, controls, layout, queue/playlist management, ad interruptions
pb = playback: playback failure, crashes, lag, connection failures, audio quality, battery/data use
dl = downloads: downloading, saved music, offline listening, disappearing downloads
ca = catalog: missing songs/artists, search and discovery, recommendations, lyrics availability
bi = billing: price, charges, subscriptions, paywalls, premium entitlement, controls that are explicitly premium-only
su = support: contacting support and the support response
ot = other: general praise or criticism, unrelated content, no supported specific topic

Topic rules
- Pick the problem with the highest severity. On a tie, the first specific problem mentioned.
- A positive review takes the first specific feature it praises; general praise is ot.
- Mentioning a paid plan is not billing by itself. A subscription that fails to activate is bi; music crashing for a paying customer is pb.
- "Bad app" with no detail is ot. Never infer a defect the review does not state.

INTENT (code = meaning, first match wins in this order)
x = cancellation: explicitly leaving, uninstalling, cancelling, switching away, or threatening to
c = complaint: a negative experience, including mixed praise and criticism
r = request: a desired change with no reported failure
p = praise
u = unclear: meaningless or unrelated text, or a bare boycott slogan with no product complaint or personal departure

SEVERITY (the reported impact, not the star rating, the anger, or the intent)
1 = no reported problem: praise, neutral or unclear content, a pure feature request
2 = dislike, generic criticism, minor annoyance, cosmetic issue; no functional loss stated
3 = a function is degraded or restricted, some use or a workaround remains
4 = a core task is clearly blocked: cannot log in, cannot play music
5 = explicit serious financial, privacy or data harm. An expensive plan, a crash or angry words alone are not 5.
Cancellation does not raise severity. If the impact is not stated, do not invent it.

Severity examples
"Best music app ever" -> 1
"Please add a sleep timer on the home screen" -> 1
"Too many ads, so annoying" -> 2
"The new layout is ugly" -> 2
"Songs keep pausing every few minutes, I have to press play again" -> 3
"Free users can only shuffle now, I can't pick the song I want" -> 3
"App closes the moment I open it, can't listen to anything" -> 4
"Won't let me log in since the update" -> 4
"Charged me three times this month and no refund" -> 5
"Someone got into my account and my card details were used" -> 5

FREE-TIER LIMIT (lim)
y = the review complains about something the free version restricts by design: ads between songs, shuffle-only play, a skip limit, not being able to pick a song or play in order, seeking, or a feature locked to premium (downloads, lyrics, offline, choosing songs). A request to remove such a limit also counts.
n = anything else: bugs, crashes, redesigns, catalog gaps, login problems, price alone, praise, unclear text.
A paying user complaining about ads in podcasts is n: that is not a free-tier limit.

PLAN (plan, what the reviewer says about their own plan)
p = says they pay or have premium, family, duo or student ("I pay for premium", "as a premium user")
f = says they use the free version or clearly that they don't pay ("on the free version", "I won't pay")
u = does not say. A complaint about premium-only features alone does not reveal the plan.

Other languages: label by meaning when you understand the review. If you do not, use ot, u, severity 1 and set rev to 1.
