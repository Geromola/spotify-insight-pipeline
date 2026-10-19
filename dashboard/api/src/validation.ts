// validation.ts — every piece of request input goes through one of these before it reaches SQL.

export type Result<T> = { ok: true; value: T } | { ok: false; message: string };

/** An issue id is `topic.subtopic`, lowercase, e.g. `billing.premium-paywall-features`. */
export const ISSUE_ID = /^[a-z][a-z0-9_-]{0,39}\.[a-z0-9][a-z0-9_-]{0,79}$/;

export const MAX_PAGE = 50;
export const DEFAULT_PAGE = 20;
export const MAX_OFFSET = 1_000_000;
export const MAX_QUERY_CHARS = 100;

export function issueId(raw: unknown): Result<string> {
  if (typeof raw !== "string" || !ISSUE_ID.test(raw)) {
    return { ok: false, message: "Issue id must look like topic.subtopic." };
  }
  return { ok: true, value: raw };
}

function single(raw: unknown): unknown {
  // Express parses ?a=1&a=2 into an array; a repeated parameter is treated as malformed.
  return Array.isArray(raw) ? undefined : raw;
}

function intParam(raw: unknown, name: string, fallback: number, min: number, max: number): Result<number> {
  const v = single(raw);
  if (v === undefined || v === "") return { ok: true, value: fallback };
  if (typeof v !== "string" || !/^\d{1,9}$/.test(v)) {
    return { ok: false, message: `${name} must be a whole number.` };
  }
  const n = Number(v);
  if (n < min) return { ok: false, message: `${name} must be at least ${min}.` };
  return { ok: true, value: Math.min(n, max) }; // values above the cap are clamped, not rejected
}

export interface ReviewQuery {
  issue: string | null;
  q: string | null;
  limit: number;
  offset: number;
}

export function reviewQuery(query: Record<string, unknown>): Result<ReviewQuery> {
  const issueRaw = single(query.issue);
  let issue: string | null = null;
  if (issueRaw !== undefined && issueRaw !== "") {
    const parsed = issueId(issueRaw);
    if (!parsed.ok) return parsed;
    issue = parsed.value;
  } else if (Array.isArray(query.issue)) {
    return { ok: false, message: "issue may be given once." };
  }

  const qRaw = single(query.q);
  if (Array.isArray(query.q) || (qRaw !== undefined && typeof qRaw !== "string")) {
    return { ok: false, message: "q may be given once." };
  }
  const q = typeof qRaw === "string" ? qRaw.trim() : "";
  if (q.length > MAX_QUERY_CHARS) {
    return { ok: false, message: `q may be at most ${MAX_QUERY_CHARS} characters.` };
  }

  const limit = intParam(query.limit, "limit", DEFAULT_PAGE, 1, MAX_PAGE);
  if (!limit.ok) return limit;
  const offset = intParam(query.offset, "offset", 0, 0, MAX_OFFSET);
  if (!offset.ok) return offset;

  return { ok: true, value: { issue, q: q || null, limit: limit.value, offset: offset.value } };
}

/** Ranking lenses stored in segment_ranking.lens; baseline is the plain issue ranking. */
export const LENSES = ["baseline", "no_free_limits", "paying"] as const;
export type Lens = (typeof LENSES)[number];

export function lens(raw: unknown): Result<Lens> {
  if (raw === undefined || raw === "") return { ok: true, value: "baseline" };
  if (typeof raw !== "string" || !(LENSES as readonly string[]).includes(raw)) {
    return { ok: false, message: `lens must be one of ${LENSES.join(", ")}.` };
  }
  return { ok: true, value: raw as Lens };
}

/** Escape LIKE wildcards so `q` is matched literally, as a substring. */
export function likePattern(q: string): string {
  return `%${q.replace(/[\\%_]/g, (c) => `\\${c}`)}%`;
}

/** The four in-scope product areas the diagnosis stage covers, in display order. */
export const DIAGNOSIS_AREAS = ["access", "usability", "playback", "billing/support"] as const;
export type DiagnosisArea = (typeof DIAGNOSIS_AREAS)[number];

/** `area` is optional (null means "use the memo's priority area"); anything outside the allow-list is rejected. */
export function diagnosisArea(raw: unknown): Result<DiagnosisArea | null> {
  if (Array.isArray(raw)) return { ok: false, message: "area may be given once." };
  if (raw === undefined || raw === "") return { ok: true, value: null };
  if (typeof raw !== "string" || !(DIAGNOSIS_AREAS as readonly string[]).includes(raw)) {
    return { ok: false, message: `area must be one of ${DIAGNOSIS_AREAS.join(", ")}.` };
  }
  return { ok: true, value: raw as DiagnosisArea };
}
