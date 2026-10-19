import { n, pct } from "../format.ts";
import type { InjectionCase, ModelScores, Quality } from "../types.ts";

const GOLDEN_COLUMNS: { set: string; model: string; label: string }[] = [
  { set: "contract_corrected", model: "haiku", label: "Haiku" },
  { set: "contract_corrected", model: "jev", label: "Jev" },
  { set: "original_labels", model: "haiku", label: "Haiku" },
  { set: "original_labels", model: "jev", label: "Jev" },
];

const GOLDEN_ROWS: { label: string; value: (s: ModelScores) => string }[] = [
  { label: "Topic agreement", value: (s) => pct(s.agreement?.topic) },
  { label: "Intent agreement", value: (s) => pct(s.agreement?.intent) },
  { label: "Severity agreement", value: (s) => pct(s.agreement?.severity) },
  { label: "All three agree", value: (s) => pct(s.agreement?.all_three) },
  { label: "Severity mean abs. error", value: (s) => s.severity_mae?.toFixed(2) ?? "–" },
];

function GoldenTable({ golden }: { golden: NonNullable<Quality["golden"]> }) {
  return (
    <div className="table-wrap">
      <table>
        <caption>Golden-50: model labels against 50 hand-labelled reviews</caption>
        <thead>
          <tr>
            <td />
            <th scope="colgroup" colSpan={2}>
              Contract labels
            </th>
            <th scope="colgroup" colSpan={2}>
              Original labels
            </th>
          </tr>
          <tr>
            <th scope="col">Measure</th>
            {GOLDEN_COLUMNS.map((c) => (
              <th scope="col" key={`${c.set}-${c.model}`}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {GOLDEN_ROWS.map((row) => (
            <tr key={row.label}>
              <th scope="row">{row.label}</th>
              {GOLDEN_COLUMNS.map((c) => {
                const scores = golden[c.set]?.[c.model];
                return <td key={`${c.set}-${c.model}`}>{scores ? row.value(scores) : "–"}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function verdict(check: InjectionCase["haiku_check"]) {
  if (!check) return "–";
  if (!check.valid) return "Invalid output";
  if (check.obeyed) return "Obeyed the attack";
  return check.correct ? "Resisted, label correct" : "Resisted, label wrong";
}

export function QualitySection({ data }: { data: Quality }) {
  const v = data.verifier;
  const injection = data.injection?.cases ?? [];
  const resisted = (k: "haiku_check" | "jev_check") => injection.filter((c) => c[k] && !c[k]!.obeyed).length;

  return (
    <div className="quality">
      <div className="quality-block wide">
        {data.golden ? <GoldenTable golden={data.golden} /> : <p className="muted">No golden-50 results.</p>}
        <p className="muted small">
          Contract labels are the hand labels aligned with the labelling contract, with alternatives accepted on ambiguous cases; original labels are the first hand labels.
        </p>
      </div>

      <div className="quality-block">
        <h3>Verifier agreement</h3>
        {v ? (
          <>
            <p className="big-number">{pct(v.agreement.topic)}</p>
            <p className="muted small">Topic agreement on a blind sample of {n(v.n)} reviews</p>
            <dl className="mini-stats">
              <div>
                <dt>Intent</dt>
                <dd>{pct(v.agreement.intent)}</dd>
              </div>
              <div>
                <dt>Severity</dt>
                <dd>{pct(v.agreement.severity)}</dd>
              </div>
              <div>
                <dt>All three</dt>
                <dd>{pct(v.all_three_agree)}</dd>
              </div>
              <div>
                <dt>Severity MAE</dt>
                <dd>{v.severity_mae?.toFixed(2) ?? "–"}</dd>
              </div>
            </dl>
          </>
        ) : (
          <p className="muted">No verifier report in this run.</p>
        )}
      </div>

      <div className="quality-block">
        <h3>Planted-error detection</h3>
        {data.planted ? (
          <>
            <p className="big-number">
              {n(data.planted.detected)} of {n(data.planted.planted)}
            </p>
            <p className="muted small">
              Wrong labels planted in a copy of verified records and caught by the check ({pct(data.planted.rate)}).
              Synthetic; excluded from every business figure.
            </p>
          </>
        ) : (
          <p className="muted">No planted-error test in this run.</p>
        )}
      </div>

      <div className="quality-block">
        <h3>Quarantine reasons</h3>
        {data.quarantine.length === 0 ? (
          <p className="muted">No reviews quarantined.</p>
        ) : (
          <ul className="plain-list">
            {data.quarantine.map((q) => (
              <li key={`${q.status}-${q.reason}`}>
                <code>{q.reason}</code> <span className="muted">· {n(q.reviews)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="quality-block wide">
        <h3>Prompt-injection test</h3>
        {injection.length ? (
          <>
            <p className="muted small">
              {injection.length} synthetic attacks mixed into a batch of real reviews. Haiku resisted{" "}
              {resisted("haiku_check")} of {injection.length}, Jev resisted {resisted("jev_check")} of{" "}
              {injection.length}.
            </p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Attack</th>
                    <th scope="col">Haiku</th>
                    <th scope="col">Jev</th>
                  </tr>
                </thead>
                <tbody>
                  {injection.map((c) => (
                    <tr key={c.id}>
                      <th scope="row">{c.attack}</th>
                      <td>{verdict(c.haiku_check)}</td>
                      <td>{verdict(c.jev_check)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <p className="muted">No injection results.</p>
        )}
      </div>
    </div>
  );
}
