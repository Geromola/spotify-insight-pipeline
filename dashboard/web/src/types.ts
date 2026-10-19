// Response shapes of the read-only API in dashboard/api/src/routes.ts.

export interface Agreement {
  n: number | null;
  topic: number | null;
  intent: number | null;
  severity: number | null;
  all_three: number | null;
  severity_mae: number | null;
}

export interface Overview {
  run_id: string | null;
  reviews: number;
  unique_texts: number | null;
  classified: number;
  quarantined: number;
  not_classified: number;
  complaints: number;
  cancellations: number;
  needs_review_share: number;
  period: { first: string | null; last: string | null };
  issues_ranked: number;
  cost: { total_usd: number; calls: number; failed: number; input_tokens: number; output_tokens: number };
  golden: (Agreement & { label_set: string; model: string }) | null;
  verifier: Agreement | null;
}

export interface FlowNode {
  id: string;
  label: string;
  descriptor: string;
  kind: "code" | "model";
  role: string;
  metric: { value: number | null; unit: string };
  provider: string | null;
  model: string | null;
  tier: string | null;
  effort: string | null;
  steps: string[];
  reads: string;
  writes: string;
  /** Extra context for the stage, e.g. which calls it counts; shown as its tooltip. */
  note: string | null;
  calls: number;
  failed: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  duration_ms: number;
}

export interface Flow {
  nodes: FlowNode[];
  totals: { calls: number; failed: number; cost_usd: number; input_tokens: number; output_tokens: number };
}

export interface Critique {
  verdict: string;
  problems: string[];
}

export interface Claim {
  claim_id: string;
  issue_id: string;
  metric: string;
  value: string;
  value_num: number | null;
  issue_title: string | null;
  rank: number | null;
  ranking_value: number | null;
  matches_ranking: boolean;
}

export interface Recommendation {
  memo: {
    question: string | null;
    ranking_rule: string | null;
    priority_area: string | null;
    headline: string | null;
    memo_markdown: string | null;
    revised: boolean | null;
    critique: Critique | null;
    final_critique: Critique | null;
    final_problems: string[];
    draft_problems: string[];
  } | null;
  claims: Claim[];
  /** A diagnosis quote (memo_v3) carries a symptom id in issue_id and resolves to `symptom` instead of an issue. */
  quotes: {
    issue_id: string;
    issue_title: string | null;
    quote: string;
    symptom: { area: DiagnosisArea; symptom_id: string; name: string | null } | null;
  }[];
  numbers_checked: boolean;
}

export interface Area {
  area: string;
  topics: string[];
  complaints: number | null;
  severity_sum: number | null;
  share_pct: number | null;
  in_question: boolean;
  free_tier_limit_complaints: number;
  other_complaints: number;
  free_tier_limit_severity_sum: number;
  other_severity_sum: number;
  paying_complaints: number;
  paying_severity_sum: number;
}

export interface Areas {
  question: string | null;
  priority_area: string | null;
  stated_plan: { paid: number; free: number; unstated: number };
  areas: Area[];
}

export interface Issue {
  issue_id: string;
  topic: string;
  subtopic: string;
  name: string | null;
  title: string | null;
  description: string | null;
  rank: number | null;
  complaint_count: number;
  severity_sum: number;
  mean_severity: number | null;
  priority_score: number | null;
  /** Counted for the baseline only; null under the other lenses. */
  cancellations: number | null;
}

export type Lens = "baseline" | "no_free_limits" | "paying";

export interface IssueList {
  lens: Lens;
  issues: Issue[];
}

export interface Review {
  review_id: string;
  review_text: string | null;
  evidence_quote: string | null;
  rating: number | null;
  likes: number | null;
  severity: number | null;
  intent: string | null;
  app_version: string | null;
  date: string | null;
  free_tier_limit?: boolean | null;
  plan?: "paid" | "free" | "unstated" | null;
  issue_id?: string | null;
  topic?: string | null;
  sentiment?: number | null;
  needs_review?: boolean | null;
}

export interface IssueDetail {
  issue: Issue & { definition: string | null; example_review_ids: string[] };
  trend: { month: string; complaints: number; completed_in_month: number | null; share: number }[];
  evidence: Review[];
  example_review_ids: string[];
}

export interface ReviewPage {
  total: number;
  limit: number;
  offset: number;
  issue: string | null;
  q: string | null;
  items: Review[];
}

export interface ModelScores {
  n: number;
  agreement: { topic: number; intent: number; severity: number; all_three: number };
  severity_mae: number;
  sentiment_mae?: number;
}

export interface InjectionCase {
  id: string;
  attack: string;
  text: string;
  haiku?: { topic: string; intent: string; severity: number };
  haiku_check?: { valid: boolean; obeyed: boolean; correct: boolean; why: string };
  jev?: { topic: string; intent: string; severity: number };
  jev_check?: { valid: boolean; obeyed: boolean; correct: boolean; why: string };
}

export interface Quality {
  golden: Record<string, Record<string, ModelScores>> | null;
  verifier: {
    n: number;
    agreement: { topic: number; intent: number; severity: number };
    all_three_agree: number | null;
    severity_mae: number;
    n_disagreements: number;
    verifier: string | null;
  } | null;
  planted: { planted: number; detected: number; rate: number; note?: string; synthetic?: boolean } | null;
  injection: { cases: InjectionCase[] } | null;
  quarantine: { reason: string; status: string; reviews: number }[];
}

export type DiagnosisArea = "access" | "usability" | "playback" | "billing/support";

export interface Symptom {
  symptom_id: string;
  name: string | null;
  definition: string | null;
  /** A control the free tier restricts by design: listed as policy, never ranked as a defect. */
  plan_gated: boolean;
  complaints: number;
  severity_sum: number;
  mean_severity: number | null;
  first6_complaints: number;
  last6_complaints: number;
  paying_complaints: number;
  likes_sum: number;
  top_context: string | null;
  top_context_share_pct: number | null;
  top_trigger: string | null;
  top_trigger_share_pct: number | null;
  /** Present only when the version has 2,000+ reviews and at least twice the symptom's overall share. */
  version?: { app_version: string; share_pct: number; overall_share_pct: number; ratio: number };
  quote: string | null;
}

export interface Diagnosis {
  area: DiagnosisArea;
  recommended: boolean;
  areas: { area: DiagnosisArea; complaints: number; recommended: boolean }[];
  coverage: {
    catalog_version: string | null;
    diagnosable_texts: number | null;
    completed: number | null;
    failed: number | null;
    other_share_pct: number | null;
  } | null;
  verification: { n: number | null; symptom: number | null; context: number | null } | null;
  customers: {
    complaints: number | null;
    long_time_users: number | null;
    family_plan_mentions: number | null;
    student_plan_mentions: number | null;
    paid: number | null;
    free: number | null;
    unstated: number | null;
  } | null;
  /** Concrete defects only, ranked by severity sum. */
  symptoms: Symptom[];
  plan_gated_symptoms: Symptom[];
  other: { complaints: number; share_pct: number | null };
  /** Only for runs whose diagnosis.json has the `concrete` block. */
  concrete?: (Record<string, number | null> & { paying_share_pct: number | null }) | null;
  concrete_by_area?: ConcreteArea[];
}

export interface ConcreteArea {
  area: DiagnosisArea;
  recommended: boolean;
  complaints: number;
  severity_sum: number;
  last6_complaints: number;
  last6_severity_sum: number;
  paying_share_pct: number | null;
}
