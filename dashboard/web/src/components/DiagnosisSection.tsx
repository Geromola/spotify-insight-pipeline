import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";

import { useApi } from "../api.ts";
import { n, sentenceCase } from "../format.ts";
import type { ConcreteArea, Diagnosis, DiagnosisArea, Symptom } from "../types.ts";

// Words the diagnose stage writes (pipeline/config.py CONTEXT_CODES and TRIGGER_CODES), as chip text.
// The brief's alternative spellings are mapped too, so a renamed code still reads well.
const CONTEXT_LABELS: Record<string, string> = {
  bluetooth: "Bluetooth",
  car: "Car",
  "tv-cast": "TV or cast",
  wearable: "Watch or wearable",
  watch: "Watch",
  "desktop-web": "Desktop or web",
  desktop: "Desktop",
};

const TRIGGER_LABELS: Record<string, string> = {
  "after-update": "after an update",
  background: "in the background",
  "screen-off": "with the screen off",
  offline: "offline",
  "app-start": "on opening the app",
  "on-open": "on opening the app",
};

const stated = (v: string | null) => Boolean(v) && v !== "none";
const label = (map: Record<string, string>, v: string) => map[v] ?? v.replace(/-/g, " ");
const pctText = (v: number | null) => (v === null ? "" : ` · ${Math.round(v)}%`);
const tabId = (area: string) => `diagnosis-tab-${area.replace(/[^a-z0-9]+/g, "-")}`;
// Lets "Billing/support" wrap after the slash inside a narrow tab.
const tabText = (area: string) => sentenceCase(area).replace("/", "/​");

const rowId = (area: string, symptomId: string) => `symptom-${area.replace(/[^a-z0-9]+/g, "-")}-${symptomId}`;

/** A symptom to open from elsewhere on the page; `seq` changes on every request so repeats still scroll. */
export interface SymptomTarget {
  area: DiagnosisArea;
  symptom_id: string;
  seq: number;
}

export function DiagnosisSection({ target = null }: { target?: SymptomTarget | null }) {
  const [chosen, setChosen] = useState<DiagnosisArea | null>(null);
  const [highlight, setHighlight] = useState<{ area: string; symptom_id: string } | null>(null);
  const pending = useRef<SymptomTarget | null>(null);
  const state = useApi<Diagnosis>(chosen ? `/api/diagnosis?area=${encodeURIComponent(chosen)}` : "/api/diagnosis");
  const tabs = useRef<Map<string, HTMLButtonElement>>(new Map());

  // While another area loads, keep showing the previous one instead of flashing an empty section.
  const last = useRef<Diagnosis | null>(null);
  if (state.status === "ready") last.current = state.data;
  const data = state.status === "ready" ? state.data : last.current;
  const loading = state.status === "loading";

  // A request from a memo quote: switch to the symptom's area, then scroll to and highlight its row.
  useEffect(() => {
    if (!target) return;
    pending.current = target;
    setChosen(target.area);
    setHighlight({ area: target.area, symptom_id: target.symptom_id });
  }, [target]);

  useEffect(() => {
    const want = pending.current;
    if (!want || loading || data?.area !== want.area) return;
    pending.current = null;
    const el = document.getElementById(rowId(want.area, want.symptom_id)) ?? document.getElementById("diagnosis");
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "center" });
    el?.focus({ preventScroll: true });
  }, [data, loading, target]);

  if (!data) {
    if (state.status === "error") {
      return (
        <p className="error" role="alert">
          Could not load the diagnosis. Please refresh the page.
        </p>
      );
    }
    return (
      <p className="muted" role="status">
        Loading the diagnosis…
      </p>
    );
  }

  const current = chosen ?? data.area;
  const areas = data.areas.map((a) => a.area);
  const select = (area: DiagnosisArea) => {
    setChosen(area);
    if (area !== highlight?.area) setHighlight(null);
    tabs.current.get(area)?.focus();
  };
  const onKey = (e: KeyboardEvent, idx: number) => {
    const next: Record<string, number> = {
      ArrowRight: (idx + 1) % areas.length,
      ArrowLeft: (idx - 1 + areas.length) % areas.length,
      Home: 0,
      End: areas.length - 1,
    };
    if (e.key in next) {
      e.preventDefault();
      select(areas[next[e.key]]);
    }
  };

  const hasAny = data.areas.some((a) => a.complaints > 0);
  const total = data.areas.find((a) => a.area === data.area)?.complaints ?? 0;
  const max = Math.max(data.other.complaints, ...data.symptoms.map((s) => s.complaints), 0);
  const isActive = (id: string) => highlight?.area === data.area && highlight.symptom_id === id;
  const v = data.verification;
  const c = data.customers;

  return (
    <div className="diagnosis">
      <p className="section-note">
        Each complaint in an area matched to one concrete symptom from an approved catalog, ranked by severity sum.
      </p>
      {data.concrete_by_area && data.concrete_by_area.length > 0 && <ConcreteComparison rows={data.concrete_by_area} />}
      <div className="segmented diagnosis-tabs" role="tablist" aria-label="Product area">
        {data.areas.map((a, idx) => {
          const selected = a.area === current;
          return (
            <button
              key={a.area}
              ref={(el) => {
                if (el) tabs.current.set(a.area, el);
                else tabs.current.delete(a.area);
              }}
              id={tabId(a.area)}
              type="button"
              role="tab"
              className="segmented-option"
              aria-selected={selected}
              aria-controls="diagnosis-panel"
              tabIndex={selected ? 0 : -1}
              onClick={() => select(a.area)}
              onKeyDown={(e) => onKey(e, idx)}
            >
              <span className="tab-label">{tabText(a.area)}</span>
              {a.recommended && <span className="tab-badge">Recommended</span>}
            </button>
          );
        })}
      </div>

      <div id="diagnosis-panel" role="tabpanel" aria-labelledby={tabId(current)} aria-busy={loading}>
        {state.status === "error" && <p className="error">Could not load this area.</p>}
        {!hasAny ? (
          <p className="muted">This run has no symptom diagnosis.</p>
        ) : total === 0 ? (
          <p className="muted">No diagnosed complaints in this area.</p>
        ) : (
          <>
            <ol className="symptom-list">
              {data.symptoms.map((s, i) => (
                <SymptomRow
                  key={s.symptom_id}
                  id={rowId(data.area, s.symptom_id)}
                  active={isActive(s.symptom_id)}
                  rank={i + 1}
                  symptom={s}
                  max={max}
                />
              ))}
            </ol>

            {data.plan_gated_symptoms.length > 0 && (
              <div className="gated-group">
                <h3>Free-tier policy, not defects</h3>
                <ul className="symptom-list">
                  {data.plan_gated_symptoms.map((s) => (
                    <li
                      key={s.symptom_id}
                      id={rowId(data.area, s.symptom_id)}
                      tabIndex={-1}
                      className={`symptom muted ${isActive(s.symptom_id) ? "active" : ""}`}
                    >
                      <div className="symptom-name">
                        <span className="symptom-rank" aria-hidden="true" />
                        <div>
                          <span className="symptom-title">{s.name ?? s.symptom_id}</span>
                          {s.definition && <span className="symptom-def">{s.definition}</span>}
                        </div>
                      </div>
                      <p className="symptom-body gated-stats">
                        <span className="badge">Free-tier policy</span>
                        <span>
                          {n(s.complaints)} {s.complaints === 1 ? "complaint" : "complaints"} · {n(s.paying_complaints)} from
                          paying reviewers
                        </span>
                      </p>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <ul className="symptom-list other-list">
              <li id={rowId(data.area, "other")} tabIndex={-1} className={`symptom muted ${isActive("other") ? "active" : ""}`}>
                <div className="symptom-name">
                  <span className="symptom-rank" aria-hidden="true" />
                  <div>
                    <span className="symptom-title">Other or not specific</span>
                    {data.other.share_pct !== null && (
                      <span className="symptom-def">{fmtShare(data.other.share_pct)} of this area's diagnosed complaints</span>
                    )}
                  </div>
                </div>
                <div className="symptom-body">
                  <Bar value={data.other.complaints} max={max} muted />
                </div>
              </li>
            </ul>
          </>
        )}

        {hasAny && (
          <div className="diagnosis-foot">
            <p>
              Symptoms assigned by Claude Haiku from a catalog Gero approved
              {v?.symptom != null && v.n != null
                ? `; Jev agrees on ${Math.round(v.symptom * 100)}% of a blind sample of ${n(v.n)}.`
                : "."}
            </p>
            {c && (
              <p>
                {sentenceCase(data.area)}: {n(c.long_time_users)} long-time {c.long_time_users === 1 ? "user" : "users"}
                {" · "}
                {n(c.family_plan_mentions)} family-plan and {n(c.student_plan_mentions)} student-plan mentions
                {" · "}
                stated plan: {n(c.paid)} paid, {n(c.free)} free, {n(c.unstated)} not stated.
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** The four areas side by side on concrete defects: severity sum over the full window and the last six months. */
function ConcreteComparison({ rows }: { rows: ConcreteArea[] }) {
  const maxFull = Math.max(...rows.map((r) => r.severity_sum), 0);
  const maxLast = Math.max(...rows.map((r) => r.last6_severity_sum), 0);
  const scale = (v: number, m: number) => `${m > 0 ? (v / m) * 100 : 0}%`;
  return (
    <figure className="concrete-compare">
      <ul>
        {rows.map((r) => (
          <li key={r.area} className={r.recommended ? "concrete-row recommended" : "concrete-row"}>
            <div className="concrete-name">
              <span>
                {sentenceCase(r.area)}
                {r.recommended && <span className="badge badge-accent concrete-badge">Recommended</span>}
              </span>
              <span className="concrete-meta">
                {n(r.complaints)} {r.complaints === 1 ? "complaint" : "complaints"}
                {r.paying_share_pct !== null && ` · ${fmtShare(r.paying_share_pct)} from paying reviewers`}
              </span>
            </div>
            <div className="concrete-bars">
              {[
                { label: "Full window", value: r.severity_sum, max: maxFull },
                { label: "Last 6 months", value: r.last6_severity_sum, max: maxLast },
              ].map((b) => (
                <div key={b.label} className="hbar">
                  <span className="hbar-label">{b.label}</span>
                  <span className="hbar-track" aria-hidden="true">
                    <span className="hbar-fill" style={{ width: scale(b.value, b.max) }} />
                  </span>
                  <span className="hbar-value">
                    {n(b.value)}
                    <span className="sr-only"> severity sum</span>
                  </span>
                </div>
              ))}
            </div>
          </li>
        ))}
      </ul>
      <figcaption>
        Concrete defects: diagnosed complaints that name a specific symptom; vague complaints and free-tier policy are
        left out. Bars show severity sum.
      </figcaption>
    </figure>
  );
}

function fmtShare(v: number) {
  return `${Number.isInteger(v) ? v : v.toFixed(1)}%`;
}

function Bar({ value, max, muted = false }: { value: number; max: number; muted?: boolean }) {
  return (
    <div className="hbar">
      <span className="hbar-label">Complaints</span>
      <span className="hbar-track" aria-hidden="true">
        <span className={`hbar-fill ${muted ? "muted-fill" : ""}`} style={{ width: `${max > 0 ? (value / max) * 100 : 0}%` }} />
      </span>
      <span className="hbar-value">{n(value)}</span>
    </div>
  );
}

function SymptomRow({
  id,
  active,
  rank,
  symptom: s,
  max,
}: {
  id: string;
  active: boolean;
  rank: number;
  symptom: Symptom;
  max: number;
}) {
  const up = s.last6_complaints > s.first6_complaints;
  const down = s.last6_complaints < s.first6_complaints;
  const chips = [
    stated(s.top_context) && `${label(CONTEXT_LABELS, s.top_context!)}${pctText(s.top_context_share_pct)}`,
    stated(s.top_trigger) && `${label(TRIGGER_LABELS, s.top_trigger!)}${pctText(s.top_trigger_share_pct)}`,
  ].filter((x): x is string => Boolean(x));
  return (
    <li id={id} tabIndex={-1} className={active ? "symptom active" : "symptom"}>
      <div className="symptom-name">
        <span className="symptom-rank">{rank}</span>
        <div>
          <strong className="symptom-title">{s.name ?? s.symptom_id}</strong>
          {s.definition && <span className="symptom-def">{s.definition}</span>}
        </div>
      </div>
      <div className="symptom-body">
        <Bar value={s.complaints} max={max} />
        <p className="symptom-stats">
          <span>Mean severity {s.mean_severity === null ? "–" : s.mean_severity.toFixed(1)}</span>
          <span>
            First 6 months → last 6 months: {n(s.first6_complaints)} → {n(s.last6_complaints)}
            {(up || down) && (
              <>
                {" "}
                <span aria-hidden="true">{up ? "↑" : "↓"}</span>
                <span className="sr-only">({up ? "up" : "down"})</span>
              </>
            )}
          </span>
          <span>Paying reviewers: {n(s.paying_complaints)}</span>
        </p>
        {(chips.length > 0 || s.version) && (
          <p className="symptom-chips">
            {chips.map((chip) => (
              <span key={chip} className="chip chip-static">
                {chip}
              </span>
            ))}
            {s.version && (
              <span className="badge badge-accent">
                {s.version.ratio}× more common in {s.version.app_version}
              </span>
            )}
          </p>
        )}
        {s.quote && <Quote text={s.quote} />}
      </div>
    </li>
  );
}

/** A quote clamped to three lines, with a toggle only when it is actually cut off. */
function Quote({ text }: { text: string }) {
  const ref = useRef<HTMLQuoteElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [clipped, setClipped] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || expanded) return;
    const check = () => setClipped(el.scrollHeight > el.clientHeight + 1);
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, [text, expanded]);

  return (
    <div className="symptom-quote">
      <blockquote ref={ref} className={expanded ? "" : "clamp-3"}>
        {text}
      </blockquote>
      {(clipped || expanded) && (
        <button type="button" className="link-button" aria-expanded={expanded} onClick={() => setExpanded((x) => !x)}>
          {expanded ? "Show less" : "Show full quote"}
        </button>
      )}
    </div>
  );
}
