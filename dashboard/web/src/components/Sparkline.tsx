import { pct } from "../format.ts";

/** Monthly share as a single line; no axes, first and last month labelled underneath. */
export function Sparkline({ points }: { points: { month: string; share: number }[] }) {
  if (points.length < 2) return <p className="muted small">Not enough full months for a trend.</p>;
  const w = 300;
  const h = 48;
  const pad = 3;
  const max = Math.max(...points.map((p) => p.share), 0.0001);
  const xy = points.map((p, i) => [
    pad + (i / (points.length - 1)) * (w - 2 * pad),
    h - pad - (p.share / max) * (h - 2 * pad),
  ]);
  const d = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const last = points[points.length - 1];
  const [lx, ly] = xy[xy.length - 1];
  return (
    <figure className="sparkline">
      <svg viewBox={`0 0 ${w} ${h}`} role="img"
        aria-label={`Monthly share of completed reviews from ${points[0].month} to ${last.month}; peak ${pct(max, 1)}, latest ${pct(last.share, 1)}`}>
        <path d={d} className="spark-line" vectorEffect="non-scaling-stroke" />
        <circle cx={lx} cy={ly} r="2.5" className="spark-dot" />
      </svg>
      <figcaption className="spark-caption">
        <span>{points[0].month}</span>
        <span>Share of completed reviews, full months · peak {pct(max, 1)}</span>
        <span>{last.month}</span>
      </figcaption>
    </figure>
  );
}
