import { n, pct, usd } from "../format.ts";
import type { Overview } from "../types.ts";

function Kpi({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="kpi">
      <p className="kpi-label">{label}</p>
      <p className="kpi-value">{value}</p>
      <p className="kpi-detail">{detail}</p>
    </div>
  );
}

export function Kpis({ overview: o }: { overview: Overview }) {
  const g = o.golden;
  return (
    <div className="kpi-row">
      <Kpi label="Classified" value={n(o.classified)} detail={`${n(o.quarantined)} quarantined`} />
      <Kpi
        label="Complaints"
        value={n(o.complaints)}
        detail={`Incl. ${n(o.cancellations)} with cancellation intent`}
      />
      <Kpi
        label="Golden-50 agreement"
        value={g ? `${pct(g.topic)} / ${pct(g.intent)} / ${pct(g.severity)}` : "–"}
        detail="Topic / intent / severity, contract labels, Haiku"
      />
      <Kpi
        label="Total API cost"
        value={usd(o.cost.total_usd)}
        detail={`${n(o.cost.calls)} calls, ${n(o.cost.failed)} failed`}
      />
    </div>
  );
}
