import { useRef, useState, type FormEvent } from "react";

import { useApi } from "../api.ts";
import { n } from "../format.ts";
import type { IssueDetail, IssueList, Lens, Review, ReviewPage } from "../types.ts";
import { Sparkline } from "./Sparkline.tsx";

const LENS_OPTIONS: { key: Lens; label: string; note: string }[] = [
  { key: "baseline", label: "All complaints", note: "Every complaint, ranked by count × mean severity." },
  {
    key: "no_free_limits",
    label: "Without free-tier limits",
    note: "Complaints about limits the free tier has by design (ads, shuffle-only, skips, premium-locked features) set aside; ranked by count × mean severity.",
  },
  {
    key: "paying",
    label: "Paying reviewers only",
    note: "Only reviewers who say they pay. Most reviewers do not state a plan, so this is a floor; ranked by count × mean severity.",
  },
];

export function IssuesSection({ selected, onSelect }: { selected: string | null; onSelect: (id: string) => void }) {
  const [lens, setLens] = useState<Lens>("baseline");
  const state = useApi<IssueList>(`/api/issues?lens=${lens}`);

  // While a new lens loads, keep showing the previous list instead of flashing an empty section.
  const last = useRef<IssueList | null>(null);
  if (state.status === "ready") last.current = state.data;
  const data = state.status === "ready" ? state.data : last.current;
  const issues = data?.issues ?? [];
  const loading = state.status === "loading";

  // The detail panel shows the chosen issue even when the current lens leaves it out; only the row
  // highlight depends on the lens. With nothing chosen yet, the lens's top issue is shown.
  const current = selected ?? issues[0]?.issue_id ?? null;
  const maxScore = Math.max(...issues.map((i) => i.priority_score ?? 0), 0);
  const option = LENS_OPTIONS.find((o) => o.key === lens)!;

  return (
    <div className="issues-layout">
      <div className="issue-list-wrap">
        <div className="segmented" role="group" aria-label="Ranking lens">
          {LENS_OPTIONS.map((o) => (
            <button
              key={o.key}
              type="button"
              className="segmented-option"
              aria-pressed={o.key === lens}
              aria-controls="issue-list"
              onClick={() => setLens(o.key)}
            >
              {o.label}
            </button>
          ))}
        </div>
        <p className="section-note lens-note">{option.note}</p>

        {state.status === "error" && <p className="error">Could not load this ranking.</p>}
        {!data && loading && (
          <p className="muted" role="status">
            Loading the issue ranking…
          </p>
        )}
        {data && issues.length === 0 && !loading && <p className="muted">No complaints in this lens.</p>}
        <ol className="issue-list" id="issue-list" aria-busy={loading}>
          {issues.map((issue) => {
            const active = issue.issue_id === current;
            const width = maxScore > 0 ? ((issue.priority_score ?? 0) / maxScore) * 100 : 0;
            return (
              <li key={issue.issue_id}>
                <button
                  type="button"
                  className={`issue-row ${active ? "active" : ""}`}
                  aria-pressed={active}
                  aria-controls="issue-detail"
                  onClick={() => onSelect(issue.issue_id)}
                >
                  <span className="issue-rank">{issue.rank}</span>
                  <span className="issue-main">
                    <span className="issue-title">{issue.title ?? issue.issue_id}</span>
                    <span className="score-track" aria-hidden="true">
                      <span className="score-fill" style={{ width: `${width}%` }} />
                    </span>
                  </span>
                  <span className="issue-stats">
                    <span>{n(issue.complaint_count)}</span>
                    <span className="muted small">sev {issue.mean_severity?.toFixed(1) ?? "–"}</span>
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      </div>
      {current ? (
        <IssuePanel key={current} issueId={current} />
      ) : (
        <div className="issue-panel muted">Select an issue to see its evidence.</div>
      )}
    </div>
  );
}

function SegmentTags({ review }: { review: Review }) {
  const tags = [
    review.free_tier_limit ? "free-tier limit" : null,
    review.plan === "paid" ? "says they pay" : review.plan === "free" ? "says free" : null,
  ].filter(Boolean) as string[];
  return (
    <>
      {tags.map((t) => (
        <span key={t} className="tag">
          {t}
        </span>
      ))}
    </>
  );
}

function IssuePanel({ issueId }: { issueId: string }) {
  const state = useApi<IssueDetail>(`/api/issues/${encodeURIComponent(issueId)}`);
  const [browsing, setBrowsing] = useState(false);

  return (
    <div className="issue-panel" id="issue-detail" aria-live="polite">
      {state.status === "loading" && <p className="muted">Loading issue…</p>}
      {state.status === "error" && <p className="error">Could not load this issue.</p>}
      {state.status === "ready" && (
        <>
          <h3>{state.data.issue.title ?? state.data.issue.issue_id}</h3>
          <p className="issue-meta">
            <code>{state.data.issue.issue_id}</code> · {n(state.data.issue.complaint_count)} complaints · priority{" "}
            {n(state.data.issue.priority_score)} · {n(state.data.issue.cancellations)} with cancellation intent
          </p>
          {state.data.issue.description && <p>{state.data.issue.description}</p>}
          <Sparkline points={state.data.trend} />

          <h4>Evidence</h4>
          <ul className="evidence">
            {state.data.evidence.map((r) => (
              <EvidenceItem key={r.review_id} review={r} />
            ))}
          </ul>

          <button
            type="button"
            className="button"
            aria-expanded={browsing}
            aria-controls="review-browser"
            onClick={() => setBrowsing((v) => !v)}
          >
            {browsing ? "Hide reviews" : "Browse all reviews for this issue"}
          </button>
          {browsing && <ReviewBrowser issueId={issueId} />}
        </>
      )}
    </div>
  );
}

function EvidenceItem({ review }: { review: Review }) {
  return (
    <li className="evidence-item">
      <blockquote>{review.evidence_quote || review.review_text || "(no text)"}</blockquote>
      <p className="evidence-meta">
        <span title={review.review_id}>{review.review_id.slice(0, 8)}</span>
        <span>severity {review.severity ?? "–"}</span>
        <span>
          {n(review.likes)} {review.likes === 1 ? "like" : "likes"}
        </span>
        <span>{review.date ?? "–"}</span>
        <SegmentTags review={review} />
      </p>
    </li>
  );
}

const PAGE = 20;

function ReviewBrowser({ issueId }: { issueId: string }) {
  const [draft, setDraft] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);

  const params = new URLSearchParams({ issue: issueId, limit: String(PAGE), offset: String(offset) });
  if (q) params.set("q", q);
  const state = useApi<ReviewPage>(`/api/reviews?${params}`);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setQ(draft.trim().slice(0, 100));
    setOffset(0);
  };

  const page = state.status === "ready" ? state.data : null;
  return (
    <div className="review-browser" id="review-browser">
      <form className="search" role="search" onSubmit={submit}>
        <label htmlFor="review-q">Search review text</label>
        <div className="search-row">
          <input
            id="review-q"
            type="search"
            value={draft}
            maxLength={100}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="e.g. shuffle"
          />
          <button type="submit" className="button">
            Search
          </button>
        </div>
      </form>
      {state.status === "loading" && <p className="muted">Loading reviews…</p>}
      {state.status === "error" && <p className="error">Could not load reviews.</p>}
      {page && (
        <>
          <p className="muted small" role="status">
            {page.total === 0
              ? "No reviews match."
              : `Showing ${n(page.offset + 1)}–${n(page.offset + page.items.length)} of ${n(page.total)}`}
          </p>
          <ul className="review-list">
            {page.items.map((r) => (
              <li key={r.review_id} className="review-item">
                <p>{r.review_text || "(no text)"}</p>
                <p className="evidence-meta">
                  <span title={r.review_id}>{r.review_id.slice(0, 8)}</span>
                  <span>{r.intent ?? "–"}</span>
                  <span>severity {r.severity ?? "–"}</span>
                  <span>rating {r.rating ?? "–"}</span>
                  <span>
                    {n(r.likes)} {r.likes === 1 ? "like" : "likes"}
                  </span>
                  <span>{r.date ?? "–"}</span>
                  <SegmentTags review={r} />
                </p>
              </li>
            ))}
          </ul>
          <div className="pager">
            <button
              type="button"
              className="button"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE))}
            >
              Previous
            </button>
            <button
              type="button"
              className="button"
              disabled={page.offset + page.items.length >= page.total}
              onClick={() => setOffset(offset + PAGE)}
            >
              Next
            </button>
          </div>
        </>
      )}
    </div>
  );
}
