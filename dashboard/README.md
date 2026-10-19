# Dashboard

A public, read-only web dashboard for a finished pipeline run. It shows saved outputs only: nothing here
calls a model. Three parts:

| Part | What it is |
|---|---|
| `db/` | `build_bundle.py` turns a run directory into `schema.sql` plus one CSV per table; `load.py` copies that bundle into Postgres (Neon). |
| `api/` | Express + TypeScript, read-only `GET /api/*` endpoints. Uses Neon when `DATABASE_URL` is set, and an in-process PGlite database loaded from the bundle otherwise. |
| `web/` | Vite + React + TypeScript single page. |

`vercel.json` deploys `web` (Vite) and `api` (Express) as two Vercel services from this folder, with `/api/*`
rewritten to the API.

## 1. Build the bundle

Standard library only. The run directory is read, never written.

```bash
python3.12 dashboard/db/build_bundle.py --run runs/full-660k-s2 --out dashboard/db/bundle
```

The run must use the s2 schema: `records.free_tier_limit` and `records.plan` in `pipeline.sqlite`,
`ranking_segments.csv`, `segments_area.csv` and `quotes` in the final memo. Older runs are refused with a
message saying what is missing. Use `runs/pilot-100-s2` for a 100-review sample. The script prints the row count of every table. The
bundle is git-ignored (`db/bundle/`).

Tables: `reviews` (one row per source review, with text, labels, `issue_id` for complaint and cancellation
reviews, `free_tier_limit`, stated `plan`, and the evidence quote), `issues`, `trends_month`, `trends_version`,
`segment_ranking` (the issue ranking under each lens: `baseline`, `no_free_limits`, `paying`), `segment_areas`
(complaints per area by free-tier limit and stated plan), `areas`, `memo` (with the memo's quotes, never
the review ids they came from), `claims`,
`stages` (model calls grouped by role and step), the diagnosis tables `diagnosis_symptoms`, `diagnosis_context`,
`diagnosis_customers` and `diagnosis_quotes` (quote text and likes only, never review ids), and `kv` (run summary,
ingestion, verifier summary, planted-error test, golden-50, injection test, classifier probe, pilot cost, and the
diagnosis catalog version, coverage and blind agreement). A run without the diagnosis files still builds, with
empty diagnosis tables and a warning.

## 2. Load it into Postgres

```bash
uv run --group dashboard python dashboard/db/load.py --bundle dashboard/db/bundle
```

`DATABASE_URL` comes from the environment or the repo `.env`. Use the Neon connection string with
`sslmode=require`. The load runs in one transaction: `schema.sql` drops and recreates the dashboard
tables, every CSV is streamed in with `COPY`, then the indexes are built. A failed load leaves the old data
in place, and running it again is safe.

## 3. Run locally

No database needed: without `DATABASE_URL` the API loads `db/bundle` into PGlite at startup (set
`BUNDLE_DIR` to use another bundle).

```bash
cd dashboard
npm run install:all
npm run dev:api     # http://localhost:3001/api/overview
npm run dev:web     # http://localhost:5173, proxies /api to :3001
```

Checks:

```bash
npm run typecheck
npm run build
npm test            # builds a fresh bundle from runs/pilot-100-s2 and tests every endpoint against PGlite
```

`npm test` needs `python3.12` on the path. `RUN_DIR=runs/<id>` tests another finalized run;
`BUNDLE_DIR=<dir>` reuses an existing bundle.

## 4. Deploy

```bash
cd dashboard
vercel            # first run links the project; vercel.json defines the web and api services
vercel --prod
```

In the Vercel project settings, add `DATABASE_URL` (the Neon connection string) as an environment variable
for Production and Preview. It is read only by the API service; nothing in `web/` sees it, and no endpoint
returns configuration.

## API

All endpoints are `GET`, parameterized SQL only, and cached at the edge with
`Cache-Control: public, s-maxage=3600`. Errors are `{ "error": { "message": "..." } }` with `no-store`.

| Endpoint | Returns |
|---|---|
| `/api/overview` | Review counts, period, quarantined, complaints, needs-review share, total API cost, golden-50 and verifier agreement |
| `/api/flow` | The nine pipeline stages with calls, failures, tokens, cost and the number shown on each node. Diagnose counts the Haiku `group`·`diagnose` calls and Jev's `verify`·`diagnose` blind check; Verify counts only Jev's checks of the enricher |
| `/api/recommendation` | Final memo, critic verdicts, every claim with its issue title and the ranking value it was checked against, and the memo's quotes with their issue titles, or with their symptom and area when the quote comes from the diagnosis (no review ids) |
| `/api/diagnosis?area=` | "What to fix" for one in-scope area (`access`, `usability`, `playback`, `billing/support`; default the memo's priority area, anything else is 400): concrete symptoms ranked by severity sum, plan-gated symptoms (free-tier policy: the run's `plan_gated` column, else `prompts/symptoms.json`) and "other" separately, `concrete_by_area` for comparing areas when the run's diagnosis.json has a `concrete` block, the stated customer mix, coverage and Jev's blind agreement. A version callout appears only when that version has 2,000+ reviews and at least twice the symptom's overall share |
| `/api/areas` | Complaints and severity sum per product area, split into free-tier-limit and other complaints, with paying-reviewer complaints and severity; plus complaints by stated plan |
| `/api/issues?lens=` | Ranked issues under a lens: `baseline` (default), `no_free_limits` or `paying`. Anything else is 400 |
| `/api/issues/:id` | One issue, its monthly share over full months, six evidence reviews (likes, then severity, then review id) and the grouper's example ids. Malformed ids are 400, unknown ids 404 |
| `/api/reviews?issue=&q=&limit=&offset=` | A page of reviews (at most 50), optionally for one issue and filtered by a case-insensitive substring `q` (at most 100 characters) |
| `/api/quality` | Golden-50 for both label sets, verifier summary, planted-error result, injection test, quarantine reasons |
