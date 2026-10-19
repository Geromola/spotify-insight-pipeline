"""Output-format probe for the s2 enricher line: which layout keeps Haiku from dropping a field?

The s2 pilot showed whole requests of 50 coming back with 9 fields instead of 10: after the new
`lim` flag the line ends in three binary digits (q|rev|lim) and the model loses count. This probe
sends the same development reviews in three layouts and counts the field-count failures.

    A  n|topic|sub|intent|sev|sent|q|rev|lim|plan   (s2 as built, lim = 1/0)
    B  n|topic|sub|intent|sev|sent|q|rev|lim|plan   (lim = y/n, so no run of three digits)
    C  n|topic|sub|intent|lim|plan|sev|sent|q|rev   (lim = y/n, letter fields together, t2 tail)

Development data only (rows 100-499 of checkpoint_500.csv, packed by length like the batcher),
every layout twice; never the golden 50. Standard synchronous tier, a few cents.

    .venv/bin/python evals/format_probe.py run         # paid; writes evals/format_probe/
    .venv/bin/python evals/format_probe.py summarize   # offline: rebuilds summary.md from responses.jsonl
"""
from __future__ import annotations

import csv
import json
import sys
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.budget import Rates  # noqa: E402
from pipeline.config import Settings, load_taxonomy  # noqa: E402
from pipeline.enrich import Item, parse, system_prompt  # noqa: E402
from pipeline.textproc import render, segments  # noqa: E402

OUT = ROOT / "evals" / "format_probe"
SAMPLE = slice(100, 500)
REPEATS = 2
PROBE_MAX_OUTPUT = 2000   # generous on purpose: measure real tokens per line, then size the cap
ARMS = ("A", "B", "C")
CANON = ("n", "topic", "sub", "intent", "sev", "sent", "q", "rev", "lim", "plan")
ORDER = {"A": CANON, "B": CANON,
         "C": ("n", "topic", "sub", "intent", "lim", "plan", "sev", "sent", "q", "rev")}


def dev_rows() -> list[dict]:
    with (ROOT / "data" / "checkpoint_500.csv").open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))[SAMPLE]


def arm_prompt(arm: str, tax: dict) -> str:
    """The pipeline's own system prompt, rewritten only where the layout differs."""
    text = system_prompt("primary", tax)
    if arm == "A":
        return text
    swaps = [
        ("1 = the review complains about something the free version", "y = the review complains about something the free version"),
        ("0 = anything else: bugs", "n = anything else: bugs"),
        ("A paying user complaining about ads in podcasts is 0:", "A paying user complaining about ads in podcasts is n:"),
        ("lim = 1 if the review is about a free-tier limit, otherwise 0", "lim = y if the review is about a free-tier limit, otherwise n"),
        ("0|ot|0|p|1|9|0|0|0|u", "0|ot|0|p|1|9|0|0|n|u"),
        ("1|dl|0|c|3|-6|1|0|0|u", "1|dl|0|c|3|-6|1|0|n|u"),
        ("2|pb|0|c|3|-7|0|0|0|u", "2|pb|0|c|3|-7|0|0|n|u"),
        ("3|us|0|c|2|-6|0|0|1|f", "3|us|0|c|2|-6|0|0|y|f"),
    ]
    if arm == "C":
        swaps += [
            ("n|topic|sub|intent|sev|sent|q|rev|lim|plan", "n|topic|sub|intent|lim|plan|sev|sent|q|rev"),
            ("0|ot|0|p|1|9|0|0|n|u", "0|ot|0|p|n|u|1|9|0|0"),
            ("1|dl|0|c|3|-6|1|0|n|u", "1|dl|0|c|n|u|3|-6|1|0"),
            ("2|pb|0|c|3|-7|0|0|n|u", "2|pb|0|c|n|u|3|-7|0|0"),
            ("3|us|0|c|2|-6|0|0|y|f", "3|us|0|c|y|f|2|-6|0|0"),
        ]
    for old, new in swaps:
        assert old in text, f"arm {arm}: prompt no longer contains {old!r}"
        text = text.replace(old, new)
    if arm == "C":  # the field list follows the new line order
        block_old = ("rev = 1 if the review lacks the context to label it confidently, cannot be understood, or has two "
                     "problems of equal severity; otherwise 0\nlim = y if the review is about a free-tier limit, "
                     "otherwise n\nplan = p f u")
        assert block_old in text, "arm C: field list changed"
        rest = block_old.split("\nlim = ")[0]
        text = text.replace(block_old, rest)
        anchor = "intent = x c r p u\n"
        text = text.replace(anchor, anchor + "lim = y if the review is about a free-tier limit, otherwise n\nplan = p f u\n")
    return text


def to_canonical(arm: str, output: str) -> str:
    """Rewrite each returned line into the s2 layout so the pipeline's own parser judges it."""
    lines = []
    for raw in output.splitlines():
        parts = [p.strip() for p in raw.strip().strip("`").split("|")]
        if len(parts) == len(CANON):
            fields = dict(zip(ORDER[arm], parts))
            if arm != "A":
                fields["lim"] = {"y": "1", "n": "0"}.get(fields["lim"], "?")
            parts = [fields[name] for name in CANON]
        lines.append("|".join(parts))
    return "\n".join(lines)


def requests_for(rows: list[dict], settings: Settings) -> list[list[Item]]:
    ordered = sorted(rows, key=lambda r: len(r["review_text"]))
    packs = []
    for start in range(0, len(ordered), settings.batch_size):
        chunk = ordered[start:start + settings.batch_size]
        packs.append([Item(i, "", r["review_text"], r["review_id"],
                           segments(r["review_text"], settings.quote_full_max)) for i, r in enumerate(chunk)])
    return packs


def send(arm: str, repeat: int, pack_no: int, items: list[Item], tax: dict, settings: Settings) -> dict:
    from pipeline.providers.anthropic_haiku import AnthropicProvider
    from pipeline.providers.base import ModelRequest

    request = ModelRequest(f"fmt-{arm}-{uuid.uuid4().hex[:8]}", "enrich", "primary", arm_prompt(arm, tax),
                           "\n".join(render(it.idx, it.segs) for it in items), settings.effort,
                           PROBE_MAX_OUTPUT, thinking=settings.thinking)
    resp = AnthropicProvider().complete(request)
    good, bad = parse(to_canonical(arm, resp.text), items, tax, resp.stop_reason, resp.refusal_category)
    return {"arm": arm, "repeat": repeat, "pack": pack_no, "ok": resp.ok, "error": resp.error,
            "stop_reason": resp.stop_reason, "n_items": len(items),
            "n_valid": len(good), "errors": dict(Counter(bad.values())),
            "labels": {items[i].origin_id: {k: good[i][k] for k in ("topic", "free_tier_limit", "plan")}
                       for i in good},
            "input_tokens": resp.input_tokens, "cached_input_tokens": resp.cached_input_tokens,
            "cache_write_tokens": resp.cache_write_tokens, "output_tokens": resp.output_tokens,
            "duration_ms": resp.duration_ms, "raw": resp.text}


def run() -> None:
    settings = Settings.load(ROOT / "config" / "haiku.toml")
    tax = load_taxonomy(settings)
    for arm in ARMS:
        arm_prompt(arm, tax)  # fail before spending if a rewrite no longer applies
    packs = requests_for(dev_rows(), settings)
    OUT.mkdir(exist_ok=True)
    path = OUT / "responses.jsonl"
    path.unlink(missing_ok=True)
    for arm in ARMS:
        # The first request of each arm writes the cache; the rest run four at a time and read it.
        first = send(arm, 0, 0, packs[0], tax, settings)
        jobs = [(r, p) for r in range(REPEATS) for p in range(len(packs)) if (r, p) != (0, 0)]
        with ThreadPoolExecutor(4) as pool:
            rest = list(pool.map(lambda job: send(arm, job[0], job[1], packs[job[1]], tax, settings), jobs))
        with path.open("a", encoding="utf-8") as f:
            for record in [first, *rest]:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"arm {arm}: done")
    summarize()


def summarize() -> None:
    rates = Rates()
    records = [json.loads(line) for line in (OUT / "responses.jsonl").read_text(encoding="utf-8").splitlines()]
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_arm[r["arm"]].append(r)
    first_labels = {arm: {} for arm in ARMS}
    for r in records:
        if r["repeat"] == 0:
            first_labels[r["arm"]].update(r["labels"])
    lines = ["# Output-format probe (s2 enricher line)", "",
             f"Development rows {SAMPLE.start}-{SAMPLE.stop - 1} of `checkpoint_500.csv`, packed by length into "
             f"requests of 50, each layout sent {REPEATS} times. Claude Haiku 5.5, thinking off, standard tier.", "",
             "| Layout | Requests | Requests with a field-count failure | Lines failing | Lines valid | "
             "Output tokens per review | Cost (standard) |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    total = 0.0
    for arm in ARMS:
        rs = by_arm.get(arm, [])
        if not rs:
            continue
        items = sum(r["n_items"] for r in rs)
        valid = sum(r["n_valid"] for r in rs)
        field_fail = sum(r["errors"].get("field_count", 0) for r in rs)
        req_fail = sum(1 for r in rs if r["errors"].get("field_count"))
        cost = sum(rates.cost("anthropic", "claude-haiku-5-5", "standard", r["input_tokens"],
                              r["cached_input_tokens"], r["output_tokens"], r["cache_write_tokens"]) for r in rs)
        total += cost
        out_per = sum(r["output_tokens"] for r in rs) / items
        worst = max(r["output_tokens"] / r["n_items"] for r in rs)
        lines.append(f"| {arm} | {len(rs)} | {req_fail} | {field_fail} of {items} | {valid} of {items} | "
                     f"{out_per:.1f} (worst request {worst:.1f}) | ${cost:.4f} |")
    lines += ["", f"Total spent: ${total:.4f}.", "",
              "Label agreement with layout A on the reviews both parsed (first repeat):", ""]
    for arm in ("B", "C"):
        shared = [rid for rid in first_labels[arm] if rid in first_labels["A"]]
        if not shared:
            continue
        agree = {k: sum(first_labels[arm][rid][k] == first_labels["A"][rid][k] for rid in shared) / len(shared)
                 for k in ("topic", "free_tier_limit", "plan")}
        lines.append(f"- {arm} vs A on {len(shared)} reviews: topic {agree['topic']:.3f}, "
                     f"free-tier limit {agree['free_tier_limit']:.3f}, plan {agree['plan']:.3f}")
    lines += ["", "Free-tier limit and plan counts (first repeat):", ""]
    for arm in ARMS:
        labels = first_labels[arm].values()
        if labels:
            lim = sum(v["free_tier_limit"] for v in labels)
            plans = Counter(v["plan"] for v in labels)
            lines.append(f"- {arm}: {lim} free-tier limit of {len(labels)}; plan {dict(plans)}")
    (OUT / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "summary.json").write_text(json.dumps({"total_usd": round(total, 6)}, indent=2), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    {"run": run, "summarize": summarize}[sys.argv[1] if len(sys.argv) > 1 else "summarize"]()
