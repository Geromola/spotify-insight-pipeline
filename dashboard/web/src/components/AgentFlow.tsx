import { useEffect, useState, type KeyboardEvent } from "react";

import { compact, n, seconds, usd } from "../format.ts";
import type { Flow, FlowNode } from "../types.ts";

// Geometry in viewBox units. The SVG is 768 wide and scales to its container.
const W = 768;
const H = 216;
const NODE_W = 128;
const NODE_H = 68;
const COL_X = [16, 168, 320, 472, 624];
const ROW_Y = [14, 134];

// Two rows in pipeline order: five stages up to Rank, then the four model stages that read the ranking.
const LAYOUT: Record<string, { col: number; row: number }> = {
  prepare: { col: 0, row: 0 },
  enrich: { col: 1, row: 0 },
  fallback: { col: 2, row: 0 },
  verify: { col: 3, row: 0 },
  rank: { col: 4, row: 0 },
  group: { col: 0, row: 1 },
  diagnose: { col: 1, row: 1 },
  memo: { col: 2, row: 1 },
  critic: { col: 3, row: 1 },
};

const right = (col: number) => COL_X[col] + NODE_W;
const midY = (row: number) => ROW_Y[row] + NODE_H / 2;
const centerX = (col: number) => COL_X[col] + NODE_W / 2;

function straight(col: number, row: number, dy = 0) {
  return `M${right(col)} ${midY(row) + dy} H${COL_X[col + 1] - 2}`;
}

const SNAKE_Y = (ROW_Y[0] + NODE_H + ROW_Y[1]) / 2;

// Every arrow, left to right; the snake carries Rank down to Group; Memo writer and Critic talk both ways.
const EDGES: { id: string; d: string; dur: number }[] = [
  { id: "prepare-enrich", d: straight(0, 0), dur: 1.4 },
  { id: "enrich-fallback", d: straight(1, 0), dur: 1.4 },
  { id: "fallback-verify", d: straight(2, 0), dur: 1.4 },
  { id: "verify-rank", d: straight(3, 0), dur: 1.4 },
  {
    id: "rank-group",
    d: `M${centerX(4)} ${ROW_Y[0] + NODE_H} V${SNAKE_Y} H${centerX(0)} V${ROW_Y[1] - 2}`,
    dur: 3.6,
  },
  { id: "group-diagnose", d: straight(0, 1), dur: 1.4 },
  { id: "diagnose-memo", d: straight(1, 1), dur: 1.4 },
  { id: "memo-critic", d: straight(2, 1, -9), dur: 1.4 },
  { id: "critic-memo", d: `M${COL_X[3]} ${midY(1) + 9} H${right(2) + 2}`, dur: 1.4 },
];

function useReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduced, setReduced] = useState(() => typeof window !== "undefined" && window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return reduced;
}

function metricText(node: FlowNode) {
  return `${n(node.metric.value)} ${node.metric.unit}`;
}

export function AgentFlow({ flow }: { flow: Flow }) {
  const [selected, setSelected] = useState<string | null>(null);
  const reduced = useReducedMotion();
  const active = flow.nodes.find((x) => x.id === selected) ?? null;

  const toggle = (id: string) => setSelected((cur) => (cur === id ? null : id));
  const onKey = (e: KeyboardEvent, id: string) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle(id);
    }
  };

  return (
    <div className="flow">
      <p className="flow-swipe" aria-hidden="true">Swipe to see the whole flow</p>
      <div className="flow-scroll">
      <svg
        className="flow-svg"
        viewBox={`0 0 ${W} ${H}`}
        role="group"
        aria-label="Pipeline stages from Prepare to Critic"
      >
        <defs>
          <marker id="flow-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0 L10 5 L0 10 z" className="flow-arrowhead" />
          </marker>
        </defs>

        {EDGES.map((e) => (
          <path key={e.id} id={`edge-${e.id}`} d={e.d} className="flow-edge" markerEnd="url(#flow-arrow)" />
        ))}

        {!reduced &&
          EDGES.map((e, i) => (
            <circle key={`dot-${e.id}`} r="3" className="flow-dot" aria-hidden="true">
              <animateMotion dur={`${e.dur}s`} begin={`-${((i * 0.35) % e.dur).toFixed(2)}s`} repeatCount="indefinite">
                <mpath href={`#edge-${e.id}`} />
              </animateMotion>
            </circle>
          ))}

        {flow.nodes.map((node) => {
          const pos = LAYOUT[node.id];
          if (!pos) return null;
          const x = COL_X[pos.col];
          const y = ROW_Y[pos.row];
          const isActive = node.id === selected;
          return (
            <g
              key={node.id}
              className={`flow-node ${node.kind} ${isActive ? "active" : ""}`}
              role="button"
              tabIndex={0}
              aria-pressed={isActive}
              aria-label={`${node.label}, ${node.descriptor}, ${metricText(node)}. Show details`}
              onClick={() => toggle(node.id)}
              onKeyDown={(e) => onKey(e, node.id)}
            >
              {node.note && <title>{node.note}</title>}
              <rect x={x} y={y} width={NODE_W} height={NODE_H} rx="10" />
              <text x={x + NODE_W / 2} y={y + 24} className="flow-label">
                {node.label}
              </text>
              <text x={x + NODE_W / 2} y={y + 41} className="flow-desc">
                {node.descriptor}
              </text>
              <text x={x + NODE_W / 2} y={y + 57} className="flow-metric">
                {metricText(node)}
              </text>
            </g>
          );
        })}
      </svg>
      </div>

      {active && <StageDetail node={active} onClose={() => setSelected(null)} />}
    </div>
  );
}

function StageDetail({ node, onClose }: { node: FlowNode; onClose: () => void }) {
  const isModel = node.kind === "model";
  return (
    <div className="flow-detail" aria-live="polite">
      <div className="flow-detail-head">
        <h3>
          {node.label} <span className="muted">· {node.descriptor}</span>
        </h3>
        <button type="button" className="link-button" onClick={onClose}>
          Close
        </button>
      </div>
      <dl className="detail-grid">
        <div>
          <dt>Role</dt>
          <dd>{node.role}</dd>
        </div>
        <div>
          <dt>Provider / model</dt>
          <dd>{isModel ? `${node.provider ?? "–"} / ${node.model ?? "–"}` : "Code, no model"}</dd>
        </div>
        <div>
          <dt>Tier</dt>
          <dd>{node.tier ?? "–"}</dd>
        </div>
        <div className="wide">
          <dt>Reads</dt>
          <dd>{node.reads}</dd>
        </div>
        <div className="wide">
          <dt>Writes</dt>
          <dd>{node.writes}</dd>
        </div>
        {node.note && (
          <div className="wide">
            <dt>Counted here</dt>
            <dd>{node.note}</dd>
          </div>
        )}
        <div>
          <dt>Calls</dt>
          <dd>{n(node.calls)}</dd>
        </div>
        <div>
          <dt>Failed calls</dt>
          <dd>{n(node.failed)}</dd>
        </div>
        <div>
          <dt>Tokens in / out</dt>
          <dd>
            {compact(node.input_tokens)} / {compact(node.output_tokens)}
          </dd>
        </div>
        <div>
          <dt>Cost</dt>
          <dd>{usd(node.cost_usd)}</dd>
        </div>
        {isModel && node.duration_ms > 0 && (
          <div>
            <dt>Model time, summed</dt>
            <dd>{seconds(node.duration_ms)}</dd>
          </div>
        )}
      </dl>
    </div>
  );
}
