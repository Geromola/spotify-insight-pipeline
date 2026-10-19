// routes.ts — the read-only dashboard endpoints. Every query is parameterized; request input is
// validated in validation.ts first. Aggregates are cast (::int, ::float8) so Neon and PGlite both
// return plain JSON numbers rather than driver-specific bigint or numeric strings.

import { Router, type Response } from "express";

import { query, type Row } from "./db.ts";
import { DIAGNOSIS_AREAS, diagnosisArea, issueId, lens, likePattern, reviewQuery } from "./validation.ts";

export const router = Router();

const CACHE = "public, s-maxage=3600";

function send(res: Response, body: unknown) {
  res.set("Cache-Control", CACHE).json(body);
}

function fail(res: Response, status: number, message: string) {
  res.set("Cache-Control", "no-store").status(status).json({ error: { message } });
}

async function kv(keys: string[]): Promise<Record<string, any>> {
  const rows = await query<{ key: string; value: unknown }>("SELECT key, value FROM kv WHERE key = ANY($1)", [keys]);
  return Object.fromEntries(rows.map((r) => [r.key, r.value]));
}

async function one<T extends Row>(sql: string, params: unknown[] = []): Promise<T | undefined> {
  return (await query<T>(sql, params))[0];
}

// ------------------------------------------------------------------ overview

const REVIEW_COUNTS = `
  SELECT count(*)::int AS reviews,
         count(*) FILTER (WHERE status = 'completed')::int AS classified,
         count(*) FILTER (WHERE status = 'quarantined')::int AS quarantined,
         count(*) FILTER (WHERE status IS DISTINCT FROM 'completed')::int AS not_classified,
         count(issue_id)::int AS complaints,
         count(*) FILTER (WHERE issue_id IS NOT NULL AND intent = 'cancellation')::int AS cancellations,
         coalesce(avg(CASE WHEN needs_review THEN 1.0 ELSE 0.0 END) FILTER (WHERE status = 'completed'), 0)::float8
           AS needs_review_share,
         to_char(min(review_ts), 'YYYY-MM-DD') AS first_review,
         to_char(max(review_ts), 'YYYY-MM-DD') AS last_review
  FROM reviews`;

const STAGE_TOTALS = `
  SELECT coalesce(sum(cost_usd), 0)::float8 AS total_usd, coalesce(sum(calls), 0)::int AS calls,
         coalesce(sum(failed), 0)::int AS failed, coalesce(sum(input_tokens), 0)::float8 AS input_tokens,
         coalesce(sum(output_tokens), 0)::float8 AS output_tokens
  FROM stages`;

function agreement(scores: any) {
  if (!scores?.agreement) return null;
  return {
    n: scores.n ?? null,
    topic: scores.agreement.topic ?? null,
    intent: scores.agreement.intent ?? null,
    severity: scores.agreement.severity ?? null,
    all_three: scores.agreement.all_three ?? scores.all_three_agree ?? null,
    severity_mae: scores.severity_mae ?? null,
  };
}

router.get("/overview", async (_req, res) => {
  const [counts, cost, ranked, store] = await Promise.all([
    one(REVIEW_COUNTS),
    one(STAGE_TOTALS),
    one("SELECT count(*)::int AS n FROM issues WHERE rank IS NOT NULL"),
    kv(["run_summary", "golden", "verify_report"]),
  ]);
  const summary = store.run_summary ?? {};
  send(res, {
    run_id: summary.run_id ?? null,
    reviews: counts?.reviews ?? 0,
    unique_texts: summary.unique_texts ?? null,
    classified: counts?.classified ?? 0,
    quarantined: counts?.quarantined ?? 0,
    not_classified: counts?.not_classified ?? 0,
    complaints: counts?.complaints ?? 0,
    cancellations: counts?.cancellations ?? 0,
    needs_review_share: counts?.needs_review_share ?? 0,
    period: { first: counts?.first_review ?? null, last: counts?.last_review ?? null },
    issues_ranked: ranked?.n ?? 0,
    cost,
    golden: store.golden?.contract_corrected?.haiku
      ? { label_set: "contract_corrected", model: "haiku", ...agreement(store.golden.contract_corrected.haiku) }
      : null,
    verifier: agreement(store.verify_report),
  });
});

// ------------------------------------------------------------------ flow

interface StageRow extends Row {
  role: string;
  step: string;
  provider: string | null;
  model: string | null;
  effort: string | null;
  tier: string | null;
  calls: number;
  failed: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  duration_ms: number;
}

type Metric = "rows" | "unique_texts" | "calls" | "issues_ranked" | "issues_named" | "texts_diagnosed";

interface NodeDef {
  id: string;
  label: string;
  descriptor: string;
  kind: "code" | "model";
  role: string;
  match: (s: StageRow) => boolean;
  metric: Metric;
  unit: string;
  reads: string;
  writes: string;
  /** Shown as the stage's tooltip and in its detail panel; built from the stage's own calls. */
  note?: (mine: StageRow[]) => string | null;
}

// Jev's blind check of the diagnoses is logged as role 'verify', step 'diagnose'. It is counted in the
// Diagnose stage, next to the Haiku calls it checks, and kept out of Verify (which checks the enricher).
const isDiagnoseCheck = (s: StageRow) => s.role === "verify" && s.step === "diagnose";
const plural = (k: number, word: string) => `${k.toLocaleString("en-US")} ${word}${k === 1 ? "" : "s"}`;

const NODES: NodeDef[] = [
  {
    id: "prepare", label: "Prepare", descriptor: "code", kind: "code", role: "prepare",
    match: () => false, metric: "rows", unit: "rows",
    reads: "The source review CSV.",
    writes: "Hashed, deduplicated rows and one backlog entry per distinct review text.",
  },
  {
    id: "enrich", label: "Enrich", descriptor: "Haiku, 50 per call", kind: "model", role: "enrich",
    match: (s) => s.role === "enrich" && s.step !== "fallback", metric: "unique_texts", unit: "unique texts",
    reads: "Batches of up to 50 distinct review texts.",
    writes: "Topic, subtopic, intent, severity, sentiment and an evidence quote per text, validated by code.",
  },
  {
    id: "fallback", label: "Fallback", descriptor: "doubtful, cap 2%", kind: "model", role: "enrich (fallback)",
    match: (s) => s.role === "enrich" && s.step === "fallback", metric: "calls", unit: "calls",
    reads: "Texts the enricher doubted or returned invalid, capped at 2% of texts.",
    writes: "A replacement label for each text, one review per call with more reasoning.",
  },
  {
    id: "verify", label: "Verify", descriptor: "Jev, blind sample", kind: "model", role: "verify",
    match: (s) => s.role === "verify" && !isDiagnoseCheck(s), metric: "calls", unit: "calls",
    reads: "A declared random sample of reviews, without the enricher's labels.",
    writes: "Independent labels that code compares with the enricher's for agreement.",
    note: () => "Counts Jev's checks of the enricher's labels only; its blind check of the diagnoses is counted in Diagnose.",
  },
  {
    id: "rank", label: "Rank", descriptor: "SQL", kind: "code", role: "rank",
    match: () => false, metric: "issues_ranked", unit: "issues",
    reads: "Complaint and cancellation reviews.",
    writes: "Issue ranking by priority score, plus monthly and app-version trends.",
  },
  {
    id: "group", label: "Group", descriptor: "Haiku names issues", kind: "model", role: "group",
    match: (s) => s.role === "group" && s.step !== "diagnose", metric: "issues_named", unit: "issues",
    reads: "Example reviews for each ranked topic.subtopic issue.",
    writes: "A short title and description per issue; code keeps the assignment of reviews.",
  },
  {
    id: "diagnose", label: "Diagnose", descriptor: "Haiku picks symptoms", kind: "model", role: "group (diagnose)",
    match: (s) => (s.role === "group" && s.step === "diagnose") || isDiagnoseCheck(s),
    metric: "texts_diagnosed", unit: "texts",
    reads: "Complaint texts in the four in-scope areas, with each area's approved symptom catalog.",
    writes: "One symptom per complaint, plus the stated device or context, the trigger and a long-time-user flag; code counts them.",
    note: (mine) => {
      const jev = mine.filter(isDiagnoseCheck).reduce((acc, s) => acc + Number(s.calls ?? 0), 0);
      return jev > 0
        ? `Includes ${plural(jev, "Jev call")} that blind-check a sample of the diagnoses; they are counted here, not in Verify.`
        : null;
    },
  },
  {
    id: "memo", label: "Memo writer", descriptor: "from aggregates", kind: "model", role: "memo",
    match: (s) => s.role === "memo" && s.step !== "critic", metric: "calls", unit: "calls",
    reads: "Aggregated facts: ranking, product areas, trends and selected quotes.",
    writes: "The recommendation memo with numbered claims that code checks against the ranking.",
  },
  {
    id: "critic", label: "Critic", descriptor: "one revision max", kind: "model", role: "memo (critic)",
    match: (s) => s.role === "memo" && s.step === "critic", metric: "calls", unit: "calls",
    reads: "The memo draft, the facts and the code's list of problems.",
    writes: "A pass or revise verdict with problems; at most one revision follows.",
  },
];

const distinct = (values: (string | null)[]) => {
  const set = new Set(values.flatMap((v) => (v ? v.split(",") : [])));
  return set.size ? [...set].join(", ") : null;
};

router.get("/flow", async (_req, res) => {
  const [stages, counts, store] = await Promise.all([
    query<StageRow>(
      `SELECT role, step, provider, model, effort, tier, calls, failed, input_tokens::float8 AS input_tokens,
              cached_input_tokens::float8 AS cached_input_tokens, output_tokens::float8 AS output_tokens,
              cost_usd, coalesce(duration_ms, 0)::float8 AS duration_ms
       FROM stages ORDER BY first_started_at NULLS LAST, role, step`,
    ),
    one(`SELECT (SELECT count(*)::int FROM reviews) AS rows,
                (SELECT count(*)::int FROM issues WHERE rank IS NOT NULL) AS issues_ranked,
                (SELECT count(*)::int FROM issues WHERE rank IS NOT NULL AND title IS NOT NULL) AS issues_named`),
    kv(["run_summary", "diagnosis_coverage"]),
  ]);
  const metrics: Record<Metric, number | null> = {
    rows: counts?.rows as number,
    unique_texts: store.run_summary?.unique_texts ?? null,
    calls: null,
    issues_ranked: counts?.issues_ranked as number,
    issues_named: counts?.issues_named as number,
    texts_diagnosed: store.diagnosis_coverage?.completed ?? null,
  };
  const nodes = NODES.map((def) => {
    const mine = stages.filter(def.match);
    const sum = (k: keyof StageRow) => mine.reduce((acc, s) => acc + Number(s[k] ?? 0), 0);
    const calls = sum("calls");
    return {
      id: def.id,
      label: def.label,
      descriptor: def.descriptor,
      kind: def.kind,
      role: def.role,
      metric: { value: def.metric === "calls" ? calls : metrics[def.metric], unit: def.unit },
      provider: distinct(mine.map((s) => s.provider)),
      model: distinct(mine.map((s) => s.model)),
      tier: distinct(mine.map((s) => s.tier)),
      effort: distinct(mine.map((s) => s.effort)),
      steps: mine.map((s) => s.step),
      reads: def.reads,
      writes: def.writes,
      note: def.note?.(mine) ?? null,
      calls,
      failed: sum("failed"),
      input_tokens: sum("input_tokens"),
      cached_input_tokens: sum("cached_input_tokens"),
      output_tokens: sum("output_tokens"),
      cost_usd: sum("cost_usd"),
      duration_ms: sum("duration_ms"),
    };
  });
  const total = (k: "calls" | "failed" | "cost_usd" | "input_tokens" | "output_tokens") =>
    stages.reduce((acc, s) => acc + Number(s[k] ?? 0), 0);
  send(res, {
    nodes,
    totals: {
      calls: total("calls"),
      failed: total("failed"),
      cost_usd: total("cost_usd"),
      input_tokens: total("input_tokens"),
      output_tokens: total("output_tokens"),
    },
  });
});

// ------------------------------------------------------------------ recommendation

router.get("/recommendation", async (_req, res) => {
  const [memo, claims] = await Promise.all([
    one(`SELECT question, ranking_rule, priority_area, headline, memo_markdown, quotes, revised,
                critique, final_critique, final_problems, draft_problems
         FROM memo WHERE id = 1`),
    query(
      `SELECT c.claim_id, c.issue_id, c.metric, c.value, i.title AS issue_title, i.rank,
              CASE c.metric
                WHEN 'complaint_count' THEN i.complaint_count::float8
                WHEN 'severity_sum' THEN i.severity_sum::float8
                WHEN 'mean_severity' THEN i.mean_severity
                WHEN 'priority_score' THEN i.priority_score
              END AS ranking_value
       FROM claims c LEFT JOIN issues i ON i.issue_id = c.issue_id
       ORDER BY c.position`,
    ),
  ]);
  const checked = claims.map((c) => {
    const value = Number(c.value);
    const ranking = c.ranking_value as number | null;
    return {
      ...c,
      value_num: Number.isFinite(value) ? value : null,
      matches_ranking: ranking !== null && Number.isFinite(value) && Math.abs(value - ranking) < 1e-6,
    };
  });
  const problems = (memo?.final_problems as unknown[] | undefined) ?? [];
  // Quotes carry the issue and the words only; the memo never exposes which review they came from.
  const { quotes: rawQuotes, ...memoFields } = memo ?? {};
  const quoteList = Array.isArray(rawQuotes) ? (rawQuotes as { issue_id?: unknown; quote?: unknown }[]) : [];
  const titles = quoteList.length
    ? await query<{ issue_id: string; title: string | null }>(
        "SELECT issue_id, title FROM issues WHERE issue_id = ANY($1)",
        [quoteList.map((q) => String(q.issue_id ?? ""))],
      )
    : [];
  const titleOf = new Map(titles.map((t) => [t.issue_id, t.title]));
  // A quote taken from the diagnosis carries a symptom id in issue_id (memo_v3). Ids that are not issues
  // are looked up as symptoms; when a symptom id exists in several areas the memo's priority area wins.
  const unknown = [...new Set(quoteList.map((q) => String(q.issue_id ?? "")).filter((id) => !titleOf.has(id)))];
  const symptomRows = unknown.length
    ? await query<{ area: string; symptom_id: string; name: string | null }>(
        "SELECT area, symptom_id, name FROM diagnosis_symptoms WHERE symptom_id = ANY($1) ORDER BY area",
        [unknown],
      )
    : [];
  const symptomOf = new Map<string, { area: string; symptom_id: string; name: string | null }>();
  for (const s of symptomRows) {
    const seen = symptomOf.get(s.symptom_id);
    if (!seen || (s.area === memo?.priority_area && seen.area !== memo?.priority_area)) symptomOf.set(s.symptom_id, s);
  }
  const quotes = quoteList
    .filter((q) => typeof q.quote === "string" && q.quote)
    .map((q) => {
      const id = String(q.issue_id ?? "");
      const symptom = titleOf.has(id) ? undefined : symptomOf.get(id);
      return {
        issue_id: id,
        issue_title: titleOf.get(id) ?? null,
        quote: q.quote as string,
        symptom: symptom ? { area: symptom.area, symptom_id: symptom.symptom_id, name: symptom.name } : null,
      };
    });
  send(res, {
    memo: memo ? memoFields : null,
    claims: checked,
    quotes,
    numbers_checked: Boolean(memo) && problems.length === 0,
  });
});

// ------------------------------------------------------------------ areas and issues

interface SegmentArea extends Row {
  area: string;
  free_tier_limit_complaints: number;
  other_complaints: number;
  free_tier_limit_severity_sum: number;
  other_severity_sum: number;
  paying_complaints: number;
  paying_severity_sum: number;
}

router.get("/areas", async (_req, res) => {
  const [areas, segments, plans, memo] = await Promise.all([
    query<{ area: string } & Row>(
      `SELECT area, topics, complaints, severity_sum, share_pct, in_question FROM areas ORDER BY position`,
    ),
    query<SegmentArea>(
      `SELECT area,
              coalesce(sum(complaints) FILTER (WHERE free_tier_limit IS TRUE), 0)::int AS free_tier_limit_complaints,
              coalesce(sum(complaints) FILTER (WHERE free_tier_limit IS NOT TRUE), 0)::int AS other_complaints,
              coalesce(sum(severity_sum) FILTER (WHERE free_tier_limit IS TRUE), 0)::int AS free_tier_limit_severity_sum,
              coalesce(sum(severity_sum) FILTER (WHERE free_tier_limit IS NOT TRUE), 0)::int AS other_severity_sum,
              coalesce(sum(complaints) FILTER (WHERE plan = 'paid'), 0)::int AS paying_complaints,
              coalesce(sum(severity_sum) FILTER (WHERE plan = 'paid'), 0)::int AS paying_severity_sum
       FROM segment_areas GROUP BY area`,
    ),
    query<{ plan: string; complaints: number }>(
      `SELECT coalesce(plan, 'unstated') AS plan, coalesce(sum(complaints), 0)::int AS complaints
       FROM segment_areas GROUP BY 1`,
    ),
    one("SELECT question, priority_area FROM memo WHERE id = 1"),
  ]);
  const byArea = new Map(segments.map((s) => [s.area, s]));
  const stated_plan = { paid: 0, free: 0, unstated: 0 };
  for (const p of plans) {
    const key = p.plan === "paid" || p.plan === "free" ? p.plan : "unstated";
    stated_plan[key] += p.complaints;
  }
  send(res, {
    question: memo?.question ?? null,
    priority_area: memo?.priority_area ?? null,
    stated_plan,
    areas: areas.map((a) => {
      const seg = byArea.get(a.area);
      return {
        ...a,
        free_tier_limit_complaints: seg?.free_tier_limit_complaints ?? 0,
        other_complaints: seg?.other_complaints ?? 0,
        free_tier_limit_severity_sum: seg?.free_tier_limit_severity_sum ?? 0,
        other_severity_sum: seg?.other_severity_sum ?? 0,
        paying_complaints: seg?.paying_complaints ?? 0,
        paying_severity_sum: seg?.paying_severity_sum ?? 0,
      };
    }),
  });
});

const ISSUE_COLUMNS = `issue_id, topic, subtopic, name, title, description, rank, complaint_count, severity_sum,
  mean_severity, priority_score, cancellations`;

router.get("/issues", async (req, res) => {
  const raw = req.query.lens;
  const chosen = lens(Array.isArray(raw) ? null : raw);
  if (!chosen.ok) return fail(res, 400, chosen.message);
  if (chosen.value === "baseline") {
    const issues = await query(`SELECT ${ISSUE_COLUMNS} FROM issues WHERE rank IS NOT NULL ORDER BY rank, issue_id`);
    return send(res, { lens: chosen.value, issues });
  }
  // Same shape as the baseline; rank and counts come from the lens. Cancellations are only counted for
  // the baseline, so they are null here rather than a misleading baseline figure.
  const issues = await query(
    `SELECT s.issue_id, coalesce(i.topic, split_part(s.issue_id, '.', 1)) AS topic,
            coalesce(i.subtopic, split_part(s.issue_id, '.', 2)) AS subtopic, i.name, i.title, i.description,
            s.rank, s.complaint_count, s.severity_sum, s.mean_severity, s.priority_score, NULL::int AS cancellations
     FROM segment_ranking s LEFT JOIN issues i ON i.issue_id = s.issue_id
     WHERE s.lens = $1 ORDER BY s.rank, s.issue_id`,
    [chosen.value],
  );
  send(res, { lens: chosen.value, issues });
});

function monthsBetween(first: string, last: string): string[] {
  const out: string[] = [];
  let [y, m] = first.split("-").map(Number);
  const [ly, lm] = last.split("-").map(Number);
  while ((y < ly || (y === ly && m <= lm)) && out.length < 600) {
    out.push(`${y}-${String(m).padStart(2, "0")}`);
    m += 1;
    if (m > 12) {
      m = 1;
      y += 1;
    }
  }
  return out;
}

router.get("/issues/:id", async (req, res) => {
  const id = issueId(req.params.id);
  if (!id.ok) return fail(res, 400, id.message);
  const issue = await one(`SELECT ${ISSUE_COLUMNS}, definition, example_review_ids FROM issues WHERE issue_id = $1`, [
    id.value,
  ]);
  if (!issue) return fail(res, 404, "No such issue.");

  const [trendRows, monthTotals, evidence] = await Promise.all([
    query<{ month: string; complaints: number; share: number }>(
      `SELECT month, complaints, share FROM trends_month
       WHERE issue_id = $1 AND partial_month IS NOT TRUE ORDER BY month`,
      [id.value],
    ),
    query<{ month: string; completed: number | null }>(
      `SELECT month, max(completed_in_month) AS completed FROM trends_month
       WHERE partial_month IS NOT TRUE GROUP BY month ORDER BY month`,
    ),
    query(
      `SELECT review_id, review_text, evidence_quote, rating, likes, severity, intent, app_version,
              free_tier_limit, plan, to_char(review_ts, 'YYYY-MM-DD') AS date
       FROM reviews WHERE issue_id = $1
       ORDER BY likes DESC NULLS LAST, severity DESC NULLS LAST, review_id
       LIMIT 6`,
      [id.value],
    ),
  ]);
  const byMonth = new Map(trendRows.map((r) => [r.month, r]));
  const completed = new Map(monthTotals.map((r) => [r.month, r.completed]));
  const months = monthTotals.length ? monthsBetween(monthTotals[0].month, monthTotals.at(-1)!.month) : [];
  const trend = months.map((month) => ({
    month,
    complaints: byMonth.get(month)?.complaints ?? 0,
    completed_in_month: completed.get(month) ?? null,
    share: byMonth.get(month)?.share ?? 0,
  }));
  send(res, { issue, trend, evidence, example_review_ids: issue.example_review_ids ?? [] });
});

// ------------------------------------------------------------------ reviews

router.get("/reviews", async (req, res) => {
  const parsed = reviewQuery(req.query as Record<string, unknown>);
  if (!parsed.ok) return fail(res, 400, parsed.message);
  const { issue, q, limit, offset } = parsed.value;

  const where: string[] = [];
  const params: unknown[] = [];
  if (issue) {
    params.push(issue);
    where.push(`issue_id = $${params.length}`);
  }
  if (q) {
    params.push(likePattern(q));
    where.push(`review_text ILIKE $${params.length} ESCAPE '\\'`);
  }
  const clause = where.length ? `WHERE ${where.join(" AND ")}` : "";
  const [count, items] = await Promise.all([
    one<{ total: number }>(`SELECT count(*)::int AS total FROM reviews ${clause}`, params),
    query(
      `SELECT review_id, review_text, evidence_quote, rating, likes, app_version,
              to_char(review_ts, 'YYYY-MM-DD') AS date, status, issue_id, topic, intent, severity,
              round(sentiment::numeric, 3)::float8 AS sentiment, needs_review, free_tier_limit, plan
       FROM reviews ${clause}
       ORDER BY likes DESC NULLS LAST, severity DESC NULLS LAST, review_id
       LIMIT $${params.length + 1} OFFSET $${params.length + 2}`,
      [...params, limit, offset],
    ),
  ]);
  send(res, { total: count?.total ?? 0, limit, offset, issue, q, items });
});

// ------------------------------------------------------------------ diagnosis

interface SymptomRow extends Row {
  symptom_id: string;
  name: string | null;
  definition: string | null;
  complaints: number;
  severity_sum: number;
  mean_severity: number | null;
  paying_complaints: number;
  first6_complaints: number;
  last6_complaints: number;
  likes_sum: number;
  top_context: string | null;
  top_context_share_pct: number | null;
  top_trigger: string | null;
  top_trigger_share_pct: number | null;
  top_version: string | null;
  top_version_share_pct: number | null;
  overall_share_pct: number | null;
  top_version_reviews: number | null;
  plan_gated: boolean | null;
  quote: string | null;
}

/** Per-area concrete-defect totals written by the diagnose stage (diagnosis.json `concrete`). */
interface ConcreteBlock {
  complaints?: number;
  severity_sum?: number;
  last6_complaints?: number;
  last6_severity_sum?: number;
  paying_complaints?: number;
  [key: string]: unknown;
}

const payingSharePct = (c: ConcreteBlock | undefined) =>
  c && typeof c.complaints === "number" && c.complaints > 0 && typeof c.paying_complaints === "number"
    ? Math.round((c.paying_complaints / c.complaints) * 1000) / 10
    : null;

/** A version is worth calling out only when it has at least this many reviews ... */
export const VERSION_MIN_REVIEWS = 2000;
/** ... and the symptom's share among them is at least this multiple of its overall share. */
export const VERSION_MIN_RATIO = 2;

const STATED_FLOOR_PCT = 10;

function stated(field: "context" | "trigger", value: string | null, share: number | null) {
  const shown = value && value !== "none" && share !== null && share >= STATED_FLOOR_PCT;
  return { [`top_${field}`]: shown ? value : null, [`top_${field}_share_pct`]: shown ? share : null };
}

export function versionCallout(row: {
  top_version: string | null;
  top_version_share_pct: number | null;
  overall_share_pct: number | null;
  top_version_reviews: number | null;
}) {
  const share = row.top_version_share_pct;
  const overall = row.overall_share_pct;
  if (!row.top_version || share === null || overall === null || !(overall > 0)) return null;
  if ((row.top_version_reviews ?? 0) < VERSION_MIN_REVIEWS || share < VERSION_MIN_RATIO * overall) return null;
  return {
    app_version: row.top_version,
    share_pct: share,
    overall_share_pct: overall,
    ratio: Math.round((share / overall) * 10) / 10,
  };
}

router.get("/diagnosis", async (req, res) => {
  const chosen = diagnosisArea(req.query.area);
  if (!chosen.ok) return fail(res, 400, chosen.message);

  const [memo, totals, store] = await Promise.all([
    one<{ priority_area: string | null }>("SELECT priority_area FROM memo WHERE id = 1"),
    query<{ area: string; complaints: number }>(
      "SELECT area, coalesce(sum(complaints), 0)::int AS complaints FROM diagnosis_symptoms GROUP BY area",
    ),
    kv(["diagnosis_coverage", "diagnosis_verify", "diagnosis_catalog_version", "diagnosis_concrete"]),
  ]);
  const totalOf = new Map(totals.map((t) => [t.area, t.complaints]));
  const priority = (DIAGNOSIS_AREAS as readonly string[]).includes(memo?.priority_area ?? "")
    ? memo!.priority_area!
    : null;
  // Without a usable memo priority, open on the area with the most diagnosed complaints.
  const fallback = [...DIAGNOSIS_AREAS].sort((a, b) => (totalOf.get(b) ?? 0) - (totalOf.get(a) ?? 0))[0];
  const area = chosen.value ?? priority ?? fallback;

  const [symptoms, customers] = await Promise.all([
    query<SymptomRow>(
      `SELECT s.symptom_id, s.name, s.definition, s.complaints, s.severity_sum, s.mean_severity,
              s.paying_complaints, s.first6_complaints, s.last6_complaints, s.likes_sum,
              s.top_context, s.top_context_share_pct, s.top_trigger, s.top_trigger_share_pct,
              s.top_version, s.top_version_share_pct, s.overall_share_pct, s.top_version_reviews,
              coalesce(s.plan_gated, false) AS plan_gated, q.quote
       FROM diagnosis_symptoms s
       LEFT JOIN diagnosis_quotes q ON q.area = s.area AND q.symptom_id = s.symptom_id AND q.rank = 1
       WHERE s.area = $1
       ORDER BY s.severity_sum DESC, s.complaints DESC, s.symptom_id`,
      [area],
    ),
    one(
      `SELECT complaints, long_time_users, family_plan_mentions, student_plan_mentions, paid, free, unstated
       FROM diagnosis_customers WHERE area = $1`,
      [area],
    ),
  ]);

  const total = totalOf.get(area) ?? 0;
  const otherRow = symptoms.find((s) => s.symptom_id === "other");
  const otherComplaints = otherRow?.complaints ?? 0;
  const coverage = store.diagnosis_coverage ?? null;
  const verify = store.diagnosis_verify ?? null;
  const concrete = (store.diagnosis_concrete ?? null) as Record<string, ConcreteBlock> | null;
  const shape = (s: SymptomRow) => ({
    symptom_id: s.symptom_id,
    name: s.name,
    definition: s.definition,
    plan_gated: Boolean(s.plan_gated),
    complaints: s.complaints,
    severity_sum: s.severity_sum,
    mean_severity: s.mean_severity,
    first6_complaints: s.first6_complaints,
    last6_complaints: s.last6_complaints,
    paying_complaints: s.paying_complaints,
    likes_sum: s.likes_sum,
    // A stated device or trigger is shown only when it covers at least 10% of the symptom (same rule as the memo).
    ...stated("context", s.top_context, s.top_context_share_pct),
    ...stated("trigger", s.top_trigger, s.top_trigger_share_pct),
    ...(versionCallout(s) ? { version: versionCallout(s) } : {}),
    quote: s.quote,
  });
  const named = symptoms.filter((s) => s.symptom_id !== "other");
  send(res, {
    area,
    recommended: area === priority,
    areas: DIAGNOSIS_AREAS.map((a) => ({ area: a, complaints: totalOf.get(a) ?? 0, recommended: a === priority })),
    coverage: coverage
      ? {
          catalog_version: store.diagnosis_catalog_version ?? null,
          diagnosable_texts: coverage.diagnosable_texts ?? null,
          completed: coverage.completed ?? null,
          failed: coverage.failed ?? null,
          other_share_pct: coverage.other_share_pct ?? null,
        }
      : null,
    verification: verify?.agreement
      ? { n: verify.n ?? null, symptom: verify.agreement.symptom ?? null, context: verify.agreement.context ?? null }
      : null,
    customers: customers ?? null,
    // Concrete defects, ranked; symptoms the free tier restricts by design are listed apart, never ranked as fixes.
    symptoms: named.filter((s) => !s.plan_gated).map(shape),
    plan_gated_symptoms: named.filter((s) => s.plan_gated).map(shape),
    // Present only for runs whose diagnosis.json has the `concrete` block.
    ...(concrete
      ? {
          concrete: concrete[area] ? { ...concrete[area], paying_share_pct: payingSharePct(concrete[area]) } : null,
          concrete_by_area: DIAGNOSIS_AREAS.map((a) => ({
            area: a,
            recommended: a === priority,
            complaints: concrete[a]?.complaints ?? 0,
            severity_sum: concrete[a]?.severity_sum ?? 0,
            last6_complaints: concrete[a]?.last6_complaints ?? 0,
            last6_severity_sum: concrete[a]?.last6_severity_sum ?? 0,
            paying_share_pct: payingSharePct(concrete[a]),
          })),
        }
      : {}),
    other: {
      complaints: otherComplaints,
      share_pct: total > 0 ? Math.round((otherComplaints / total) * 1000) / 10 : null,
    },
  });
});

// ------------------------------------------------------------------ quality

router.get("/quality", async (_req, res) => {
  const [store, quarantine] = await Promise.all([
    kv(["golden", "verify_report", "verify_planted", "injection"]),
    query(
      `SELECT coalesce(reason, '(no reason recorded)') AS reason, coalesce(status, 'unknown') AS status,
              count(*)::int AS reviews
       FROM reviews WHERE status IS DISTINCT FROM 'completed'
       GROUP BY 1, 2 ORDER BY 3 DESC, 1`,
    ),
  ]);
  send(res, {
    golden: store.golden ?? null,
    verifier: store.verify_report ?? null,
    planted: store.verify_planted ?? null,
    injection: store.injection ?? null,
    quarantine,
  });
});
