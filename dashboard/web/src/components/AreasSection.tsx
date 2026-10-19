import { n, sentenceCase } from "../format.ts";
import type { Areas } from "../types.ts";

/** A total split into two segments of one hue: solid for other complaints, lighter for free-tier limits. */
function SplitBar({
  label,
  unit,
  total,
  other,
  free,
  max,
  muted,
}: {
  label: string;
  unit: string;
  total: number | null;
  other: number;
  free: number;
  max: number;
  muted: boolean;
}) {
  const scale = (v: number) => (max > 0 ? (v / max) * 100 : 0);
  return (
    <div className="hbar">
      <span className="hbar-label">{label}</span>
      <span className="hbar-track hbar-split" aria-hidden="true">
        <span className={`hbar-fill ${muted ? "muted-fill" : ""}`} style={{ width: `${scale(other)}%` }} />
        <span className={`hbar-fill hbar-free ${muted ? "muted-fill" : ""}`} style={{ width: `${scale(free)}%` }} />
      </span>
      <span className="hbar-value">
        {n(total)}
        <span className="sr-only">
          {" "}
          {unit}: {n(other)} from other complaints, {n(free)} from free-tier limits
        </span>
      </span>
    </div>
  );
}

export function AreasSection({ data }: { data: Areas }) {
  if (!data.areas.length) return <p className="muted">No product-area figures in this run.</p>;
  const maxComplaints = Math.max(...data.areas.map((a) => a.complaints ?? 0));
  const maxSeverity = Math.max(...data.areas.map((a) => a.severity_sum ?? 0));
  return (
    <>
      {data.question && <p className="section-note">{data.question}</p>}
      <ul className="legend" aria-label="Bar legend">
        <li>
          <span className="swatch" aria-hidden="true" /> Other complaints
        </li>
        <li>
          <span className="swatch swatch-free" aria-hidden="true" /> Free-tier limits
        </li>
      </ul>
      <ul className="areas">
        {data.areas.map((a) => {
          const muted = !a.in_question;
          return (
            <li key={a.area} className={muted ? "area muted" : "area"}>
              <div className="area-name">
                <span>{sentenceCase(a.area)}</span>
                <span className="area-topics">{a.topics.join(", ")}</span>
              </div>
              <div className="area-bars">
                <SplitBar
                  label="Complaints"
                  unit="complaints"
                  total={a.complaints}
                  other={a.other_complaints}
                  free={a.free_tier_limit_complaints}
                  max={maxComplaints}
                  muted={muted}
                />
                <SplitBar
                  label="Severity sum"
                  unit="severity sum"
                  total={a.severity_sum}
                  other={a.other_severity_sum}
                  free={a.free_tier_limit_severity_sum}
                  max={maxSeverity}
                  muted={muted}
                />
                <p className="area-paying">
                  Without free-tier limits: {n(a.other_complaints)} complaints, severity {n(a.other_severity_sum)}
                  {" · "}
                  Paying reviewers: {n(a.paying_complaints)} {a.paying_complaints === 1 ? "complaint" : "complaints"}
                </p>
              </div>
            </li>
          );
        })}
      </ul>
    </>
  );
}
