// A deliberately small Markdown renderer for the memo. It builds React elements only (headings,
// paragraphs, lists, inline code, bold and italics) and never injects HTML, so model-written text cannot
// add markup to the page. Claim citations such as [C1] are handed to `renderClaim`.

import { Fragment, type ReactNode } from "react";

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] };

export function parseBlocks(md: string): Block[] {
  const blocks: Block[] = [];
  let para: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  const flush = () => {
    if (para.length) blocks.push({ kind: "paragraph", text: para.join(" ") });
    if (list) blocks.push({ kind: "list", ...list });
    para = [];
    list = null;
  };
  for (const raw of md.replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.trim();
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    const bullet = /^[-*+]\s+(.*)$/.exec(line);
    const numbered = /^\d+[.)]\s+(.*)$/.exec(line);
    if (!line) {
      flush();
    } else if (heading) {
      flush();
      blocks.push({ kind: "heading", level: heading[1].length, text: heading[2] });
    } else if (bullet || numbered) {
      const ordered = Boolean(numbered);
      if (para.length || (list && list.ordered !== ordered)) flush();
      list ??= { ordered, items: [] };
      list.items.push((bullet ?? numbered)![1]);
    } else if (list && /^\s{2,}/.test(raw)) {
      list.items[list.items.length - 1] += ` ${line}`; // continuation of a list item
    } else {
      if (list) flush();
      para.push(line);
    }
  }
  flush();
  return blocks;
}

const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\s][^*]*\*|\[C\d+\])/g;

function inline(text: string, renderClaim: (id: string, key: string) => ReactNode): ReactNode[] {
  return text.split(INLINE).map((part, i) => {
    const key = String(i);
    if (!part) return null;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 1) return <code key={key}>{part.slice(1, -1)}</code>;
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={key}>{part.slice(2, -2)}</strong>;
    if (/^\[C\d+\]$/.test(part)) return <Fragment key={key}>{renderClaim(part.slice(1, -1), key)}</Fragment>;
    if (part.startsWith("*") && part.endsWith("*") && part.length > 2) return <em key={key}>{part.slice(1, -1)}</em>;
    return <Fragment key={key}>{part}</Fragment>;
  });
}

export function Markdown({
  blocks,
  headingOffset = 1,
  renderClaim,
}: {
  blocks: Block[];
  headingOffset?: number;
  renderClaim: (id: string, key: string) => ReactNode;
}) {
  return (
    <div className="markdown">
      {blocks.map((b, i) => {
        if (b.kind === "heading") {
          const level = Math.min(6, b.level + headingOffset);
          const Tag = `h${level}` as "h3";
          return <Tag key={i}>{inline(b.text, renderClaim)}</Tag>;
        }
        if (b.kind === "list") {
          const Tag = b.ordered ? "ol" : "ul";
          return (
            <Tag key={i}>
              {b.items.map((item, j) => (
                <li key={j}>{inline(item, renderClaim)}</li>
              ))}
            </Tag>
          );
        }
        return <p key={i}>{inline(b.text, renderClaim)}</p>;
      })}
    </div>
  );
}
