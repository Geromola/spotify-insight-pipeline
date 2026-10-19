const int = new Intl.NumberFormat("en-US");

export function n(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : int.format(value);
}

export function pct(value: number | null | undefined, digits = 0): string {
  return value === null || value === undefined ? "–" : `${(value * 100).toFixed(digits)}%`;
}

export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  if (value === 0) return "$0";
  if (value < 1) return `$${value.toPrecision(3)}`;
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function compact(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

export function seconds(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

const METRIC_LABELS: Record<string, string> = {
  complaint_count: "complaints",
  severity_sum: "severity sum",
  mean_severity: "mean severity",
  priority_score: "priority score",
};

export function metricLabel(metric: string): string {
  return METRIC_LABELS[metric] ?? metric.replace(/_/g, " ");
}

/** Trim trailing zeros from values the pipeline writes as fixed decimals, e.g. "2.000000" -> "2". */
export function claimValue(value: string): string {
  const num = Number(value);
  if (!Number.isFinite(num)) return value;
  return Number.isInteger(num) ? int.format(num) : String(Math.round(num * 100) / 100);
}

export function sentenceCase(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}
