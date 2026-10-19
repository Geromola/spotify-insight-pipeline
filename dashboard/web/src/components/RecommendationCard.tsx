import { useMemo, useState } from "react";

import { claimValue, metricLabel, sentenceCase } from "../format.ts";
import type { Claim, DiagnosisArea, Recommendation } from "../types.ts";
import { Markdown, parseBlocks } from "./Markdown.tsx";

export function RecommendationCard({
  data,
  onSelectIssue,
  onSelectSymptom,
}: {
  data: Recommendation;
  onSelectIssue: (issueId: string) => void;
  onSelectSymptom: (area: DiagnosisArea, symptomId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const memo = data.memo;
  // The writer sometimes runs its sections together as bold lead-ins ("**Why.** ..."); start each on its own paragraph.
  const blocks = useMemo(
    () => parseBlocks((memo?.memo_markdown ?? "").replace(/\s+(\*\*[A-Z][a-z]+\.\*\*)/g, "\n\n$1")),
    [memo?.memo_markdown],
  );
  const claims = useMemo(() => new Map(data.claims.map((c) => [c.claim_id, c])), [data.claims]);

  if (!memo) return <p className="muted">This run has no final memo.</p>;

  // Collapsed, the memo shows its first two sections (Recommendation and What to fix first): everything before
  // the third heading. The concrete fixes are the point of the memo, so they never hide behind the toggle.
  const headingIdx = blocks.flatMap((b, i) => (b.kind === "heading" ? [i] : []));
  const cut = headingIdx.length > 2 ? headingIdx[2] : headingIdx.length > 1 ? headingIdx[1] : Math.min(blocks.length, 2);
  const visible = expanded ? blocks : blocks.slice(0, cut);
  const canExpand = cut < blocks.length;

  const renderClaim = (id: string, key: string) => {
    const claim = claims.get(id);
    if (!claim) return <span key={key} className="chip chip-static">{id}</span>;
    return <ClaimChip key={key} claim={claim} onSelect={onSelectIssue} />;
  };

  return (
    <div className="recommendation">
      {memo.headline && <p className="headline">{memo.headline}</p>}
      {memo.priority_area && (
        <p className="priority">
          Recommended priority: <strong>{sentenceCase(memo.priority_area)}</strong>
        </p>
      )}
      <div className="badges">
        {data.numbers_checked && <span className="badge badge-accent">Every number checked by code</span>}
        {memo.revised ? (
          <>
            {memo.critique?.verdict && <span className="badge">Critic on the draft: {memo.critique.verdict}</span>}
            <span className="badge">Revised once</span>
            {memo.final_critique?.verdict && (
              <span className="badge">
                {memo.final_critique.verdict === "pass"
                  ? "Critic on the final memo: pass"
                  : `Critic's final notes recorded (${memo.final_critique.problems?.length ?? 0})`}
              </span>
            )}
          </>
        ) : (
          (memo.final_critique ?? memo.critique)?.verdict && (
            <span className="badge">Critique: {(memo.final_critique ?? memo.critique)!.verdict}</span>
          )
        )}
      </div>

      <div id="memo-body">
        <Markdown blocks={visible} headingOffset={1} renderClaim={renderClaim} />
      </div>
      {canExpand && (
        <button
          type="button"
          className="link-button"
          aria-expanded={expanded}
          aria-controls="memo-body"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "Show less" : "Read full memo"}
        </button>
      )}

      {data.quotes.length > 0 && (
        <div className="quotes">
          <h3>Quotes</h3>
          <ul className="quote-list">
            {data.quotes.map((q, i) => {
              const symptom = q.symptom;
              return (
                <li key={i} className="quote-item">
                  <blockquote>{q.quote}</blockquote>
                  {symptom ? (
                    <button
                      type="button"
                      className="chip"
                      aria-label={`Show symptom in What to fix: ${symptom.name ?? symptom.symptom_id}`}
                      onClick={() => onSelectSymptom(symptom.area, symptom.symptom_id)}
                    >
                      {symptom.name ?? symptom.symptom_id}
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="chip"
                      aria-label={`Show issue: ${q.issue_title ?? q.issue_id}`}
                      onClick={() => onSelectIssue(q.issue_id)}
                    >
                      {q.issue_title ?? q.issue_id}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

function ClaimChip({ claim, onSelect }: { claim: Claim; onSelect: (issueId: string) => void }) {
  const label = `${metricLabel(claim.metric)} ${claimValue(claim.value)}`;
  return (
    <button
      type="button"
      className="chip"
      title={`${claim.claim_id}: ${claim.issue_title ?? claim.issue_id}`}
      aria-label={`Claim ${claim.claim_id}, ${label}, for ${claim.issue_title ?? claim.issue_id}. Show this issue`}
      onClick={() => onSelect(claim.issue_id)}
    >
      <span className="chip-id">{claim.claim_id}</span> {label}
    </button>
  );
}
