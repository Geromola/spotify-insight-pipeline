"""100-review cost and runtime calculator.

Default: OFFLINE REPLAY. Recomputes every cost from saved usage × editable rates and projects
the full run. It needs no API key and makes no model call:

    python3.12 cost/calculator.py
    python3.12 cost/calculator.py --rate-multiplier 2      # API subtotal doubles, time does not
    python3.12 cost/calculator.py --unique-texts 300000    # projection moves, measurements do not

Paid execution is a separate, explicit command:

    python3.12 cost/calculator.py pilot --paid --config config/pilot.toml --run pilot-100
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
UNITS = {"per_1M_tokens": 1_000_000, "per_token": 1}
DEFAULTS = {  # editable from the command line; nothing here changes a measured number
    "rows": 660_622, "nonempty": 660_609, "empty": 13, "unique_texts": 484_189,
    "full_mean_chars": 47_724_767 / 484_189,  # measured over the whole corpus by `prepare`
    "full_tier": "batch", "workers": 1, "budget": 15.0, "output_token_cap": 1400, "batch_size": 50,
    "fallback_fraction": 0.02, "max_fallback_fraction": 0.02, "retry_rate": 0.01, "verify_sample": 1000,
    "diagnosable_texts": 0,  # 0 = scale the unique texts by the pilot's ratio of diagnosable to unique texts
    "diagnose_check_sample": 300,
}


def load_rates(path: Path, multiplier: float) -> dict:
    with path.open(encoding="utf-8", newline="") as f:
        return {(r["provider"], r["model"], r["tier"], r["item"]): float(r["price_usd"]) / UNITS[r["unit"]] * multiplier
                for r in csv.DictReader(f)}


def measured(usage_path: Path, rates: dict) -> dict:
    """item_cost = billed_units × price_per_unit, summed per run and stage."""
    stages: dict = defaultdict(lambda: defaultdict(float))
    with usage_path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            stage = stages[(row["run"], row["role"], row["step"], row["provider"], row["model"], row["tier"])]
            units = int(row["billed_units"])
            stage[row["item"]] += units
            stage["cost"] += units * rates[(row["provider"], row["model"], row["tier"], row["item"])]
            if row["item"] == "input":
                stage["requests"] += 1
                stage["texts"] += int(row["texts_in_request"])
                stage["usage_missing"] += 1 - int(row["usage_available"])
    return stages


def project(stages: dict, pilot: dict, rates: dict, a: dict) -> list[dict]:
    """Scale each stage by its own work count. Fixed overhead is added once, never per review."""
    cold = {key[2]: (key, stage) for key, stage in stages.items() if key[0] == "cold" and key[1] == "enrich"}
    if "primary" not in cold:
        return []
    (_, _, _, provider, model, tier), primary = cold["primary"]
    length = a["full_mean_chars"] / pilot["mean_chars_per_unique_text"]
    # Split measured input into the static prefix (instructions, paid per request) and each text's own
    # tokens (paid per text, scaled by text length). Output is a fixed set of short fields per text.
    prefix_per_request = (primary["cached_input"] + primary["cache_write"]) / primary["requests"]
    own_in_per_text = primary["input"] / primary["texts"]
    per_text_out = min(primary["output"] / primary["texts"], a["output_token_cap"])
    seconds_per_text = pilot["cold"]["stage_seconds"]["enrich"] / max(1, pilot["unique_texts"])
    fallback = cold.get("fallback")
    # The verifier scales with its declared sample, not with the corpus; group and memo run once.
    # The diagnose stage (classifier and its blind check) is projected on its own line below.
    verify = [s for k, s in stages.items() if k[0] == "cold" and k[1] == "verify" and k[2] != "diagnose"]
    verify_per_text = sum(s["cost"] for s in verify) / sum(s["texts"] for s in verify) if verify else None
    writers = [s for k, s in stages.items() if k[0] == "cold" and k[1] in ("group", "memo") and k[2] != "diagnose"]
    fixed = sum(s["cost"] for s in writers) if writers else None
    diagnose = [(k, s) for k, s in stages.items() if k[0] == "cold" and k[1] == "group" and k[2] == "diagnose"]
    check = [s for k, s in stages.items() if k[0] == "cold" and k[1] == "verify" and k[2] == "diagnose"]
    diagnosed = pilot.get("diagnosed_texts") or sum(s["texts"] for _, s in diagnose)
    diagnosable = a["diagnosable_texts"] or (round(a["unique_texts"] * pilot["diagnosable_texts"] / pilot["unique_texts"])
                                             if pilot.get("diagnosable_texts") else 0)
    scenarios = []
    for name, texts, tier_used, retry, fallback_fraction, cache in (
            ("base: exact-text reuse", a["unique_texts"], a["full_tier"], a["retry_rate"], a["fallback_fraction"], True),
            ("no reuse: every nonempty row", a["nonempty"], a["full_tier"], a["retry_rate"], a["fallback_fraction"], True),
            ("conservative: standard tier, 5% retries, max fallback, no prompt cache", a["unique_texts"], tier,
             0.05, a["max_fallback_fraction"], False)):
        price = lambda item, t=tier_used: rates[(provider, model, t, item)]
        work = texts * (1 + retry)
        requests = -(-work // a["batch_size"])
        # With caching, the prefix is a cache read on every request once warm; one write per cache
        # window is negligible over thousands of requests. Without caching it is ordinary input.
        prefix_tokens, own_tokens = requests * prefix_per_request, work * own_in_per_text * length
        tokens_in, tokens_out = prefix_tokens + own_tokens, work * per_text_out
        enrich = (prefix_tokens * price("cached_input" if cache else "input") + own_tokens * price("input")
                  + tokens_out * price("output"))
        if fallback:  # one text per fallback request, priced at this scenario's tier
            usage = fallback[1]
            per_fallback = (usage["input"] * price("input") + usage["cache_write"] * price("cache_write")
                            + usage["cached_input"] * price("cached_input") + usage["output"] * price("output")) / usage["texts"]
            fallback_cost, fallback_note = texts * fallback_fraction * per_fallback, "measured tokens per text"
        else:
            fallback_cost, fallback_note = None, "UNKNOWN: no fallback call was measured in the pilot"
        verify_cost = verify_per_text * a["verify_sample"] if verify_per_text is not None else None
        overhead = None if verify_cost is None or fixed is None else verify_cost + fixed
        if diagnose and diagnosed:  # measured tokens per diagnosed text, repriced at this scenario's tier
            (_, _, _, d_provider, d_model, _), usage = diagnose[0]
            d_price = lambda item, t=tier_used: rates[(d_provider, d_model, t, item)]
            per_diagnosis = (usage["input"] * d_price("input") + usage["cache_write"] * d_price("cache_write")
                             + usage["cached_input"] * d_price("cached_input" if cache else "input")
                             + usage["output"] * d_price("output")) / diagnosed
            check_cost = (sum(s["cost"] for s in check) / sum(s["texts"] for s in check)
                          * min(a["diagnose_check_sample"], diagnosable)) if check else 0.0
            diagnose_cost = diagnosable * per_diagnosis + check_cost
            diagnose_note = (f"{diagnosable:,} diagnosable texts x {money_note(per_diagnosis)} per diagnosed text "
                             f"(pilot: {pilot.get('diagnosable_texts', 0)} diagnosable of {pilot['unique_texts']} unique)"
                             + (f" + blind check {money_note(check_cost)}" if check else " (blind check not measured)"))
        else:
            diagnose_cost, diagnose_note = None, "UNKNOWN: the diagnose stage was not measured in the pilot"
        total = enrich + (fallback_cost or 0) + (overhead or 0) + (diagnose_cost or 0)
        scenarios.append({
            "scenario": name, "texts_to_classify": texts, "rows_accounted": a["rows"], "empty_quarantined": a["empty"],
            "tier": tier_used, "tier_measured": tier_used == tier, "retry_rate": retry,
            "fallback_fraction": fallback_fraction, "enrich_input_tokens": round(tokens_in),
            "enrich_output_tokens": round(tokens_out), "enrich_usd": enrich, "fallback_usd": fallback_cost,
            "fallback_note": fallback_note, "fixed_overhead_usd": overhead,
            "fixed_overhead_note": (f"verifier: measured cost per text x {a['verify_sample']:,} declared sample; "
                                    "group and memo: measured once on the pilot, added once")
                                   if overhead is not None else "UNKNOWN: verify, group or memo not measured",
            "diagnose_usd": diagnose_cost, "diagnose_note": diagnose_note, "diagnosable_texts": diagnosable,
            "api_total_usd": total, "local_compute_usd": 0.0, "over_budget": total > a["budget"],
            "sync_hours_modeled": work * seconds_per_text / a["workers"] / 3600,
        })
    return scenarios


def money_note(v: float) -> str:
    return f"${v:,.6f}" if v < 1 else f"${v:,.2f}"


def report(pilot: dict, stages: dict, scenarios: list[dict], a: dict, multiplier: float) -> str:
    money = lambda v: "unknown" if v is None else f"${v:,.6f}" if v < 1 else f"${v:,.2f}"
    lines = ["# 100-review cost and runtime calculator", ""]
    if pilot["synthetic"]:
        lines += ["> **SYNTHETIC RUN — NOT A MEASUREMENT.** Produced by the offline fake provider to test the",
                  "> calculator's arithmetic. Token counts are character estimates and prices are placeholders.", ""]
    lines += [f"Input `{pilot['input_file']}` · sha256 `{pilot['input_sha256']}`", "",
              f"Label config: `{pilot['label_config']}`", "",
              f"Rate multiplier applied: ×{multiplier:g} (rates come from `rates.csv`, with dated source links)", "",
              "## Measured on the pilot", "",
              f"- Records: {pilot['records']} · completed {pilot['completed']} · failed/quarantined {pilot['failed']}",
              f"- Unique texts: {pilot['unique_texts']} · result-cache hits: {pilot['result_cache_hits']} · "
              f"fallback texts: {pilot['fallback_texts']}",
              f"- Workers: {pilot['settings']['workers']} · batch size limit: {pilot['settings']['batch_size']} · "
              f"mode: {pilot['settings']['mode']}",
              f"- Stages included: {', '.join(pilot['stages_included'])} · not yet included: "
              f"{', '.join(pilot['stages_not_yet_included']) or 'none'}", "",
              "| Run | Stage | Model · tier | Requests | Texts | Uncached input | Cache writes | Cache reads | Output | Cost | Usage missing |",
              "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    totals = defaultdict(float)
    for (run, role, step, provider, model, tier), s in sorted(stages.items()):
        totals[run] += s["cost"]
        lines.append(f"| {run} | {role}·{step} | {provider}/{model} · {tier} | {int(s['requests'])} | {int(s['texts'])} | "
                     f"{int(s['input'])} | {int(s['cache_write'])} | {int(s['cached_input'])} | {int(s['output'])} | {money(s['cost'])} | "
                     f"{int(s['usage_missing'])} |")
    lines += ["", "| Run | Wall-clock s | Model calls | API cost | Cost per 1,000 input rows | Cost per completed record | Rows per second |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for run in ("cold", "warm"):
        p, cost = pilot[run], totals.get(run, 0.0)
        lines.append(f"| {run} | {p['wall_seconds']} | {p['model_calls']} | {money(cost)} | "
                     f"{money(cost / pilot['records'] * 1000)} | {money(cost / max(1, pilot['completed']))} | "
                     f"{pilot['records'] / p['wall_seconds']:.1f} |")
    lines += ["", f"Warm run: **{pilot['warm']['new_enrichment_calls']} new enrichment calls** under unchanged settings.",
              "", "Stage seconds (cold): " + ", ".join(f"{k} {v}" for k, v in pilot["cold"]["stage_seconds"].items()),
              "", "Local compute: $0 assumed (a laptop running SQLite and Python); electricity and hardware are not measured.",
              "", "## Estimated before the full run", "",
              f"Assumptions (all editable flags): rows {a['rows']:,}; nonempty {a['nonempty']:,}; empty-text quarantines "
              f"{a['empty']}; unique texts {a['unique_texts']:,}; verification sample {a['verify_sample']:,}; output-token "
              f"cap {a['output_token_cap']}; max workers {a['workers']}; max fallback fraction "
              f"{a['max_fallback_fraction']:.0%}; budget ${a['budget']:.2f}.", "",
              "Model: the measured instruction prefix is paid once per request of up to 50 texts (a cache read once "
              "warm, ordinary input when uncached); each text's own input tokens are scaled by the ratio of mean "
              "text length (full corpus ÷ pilot); output is a fixed set of short fields per text.", "",
              "| Scenario | Texts | Tier | Retries | Fallback | Enrich | Fallback cost | Fixed overhead | Diagnose | API total | Sync hours (modeled) | Budget |",
              "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for s in scenarios:
        tier = s["tier"] + ("" if s["tier_measured"] else " (ESTIMATED: tier not measured)")
        lines.append(f"| {s['scenario']} | {s['texts_to_classify']:,} | {tier} | {s['retry_rate']:.0%} | "
                     f"{s['fallback_fraction']:.0%} | {money(s['enrich_usd'])} | {money(s['fallback_usd'])} | "
                     f"{money(s['fixed_overhead_usd'])} | {money(s['diagnose_usd'])} | {money(s['api_total_usd'])} | "
                     f"{s['sync_hours_modeled']:.2f} | "
                     f"{'**OVER BUDGET**' if s['over_budget'] else 'within'} |")
    if not scenarios:
        lines.append("| no primary enrichment usage was measured, so nothing can be projected | | | | | | | | | | | |")
    if scenarios:
        lines += ["", f"Fixed overhead: {scenarios[0]['fixed_overhead_note']}."]
        lines += ["", f"Diagnose stage (base scenario): {scenarios[0]['diagnose_note']}."]
    lines += ["", "Sync hours are modeled from the pilot's one-worker throughput divided by the worker count; more workers "
              "shorten elapsed time but do not lower cost. Batch API turnaround (up to 24 h) is not modeled.", ""]
    return "\n".join(lines)


def replay(args) -> int:
    pilot_dir = args.pilot_dir
    needed = [pilot_dir / name for name in ("pilot_runs.json", "usage.csv")]
    if not all(p.exists() for p in needed):
        print(f"No saved pilot in {pilot_dir}. Nothing has been measured yet, so there is nothing to replay.\n"
              "Run the paid pilot explicitly:  python3.12 cost/calculator.py pilot --paid --config <settings.toml> --run <id>")
        return 0
    rates = load_rates(args.rates, args.rate_multiplier)
    pilot = json.loads((pilot_dir / "pilot_runs.json").read_text(encoding="utf-8"))
    assumptions = {key: getattr(args, key) for key in DEFAULTS}
    stages = measured(pilot_dir / "usage.csv", rates)
    scenarios = project(stages, pilot, rates, assumptions)
    text = report(pilot, stages, scenarios, assumptions, args.rate_multiplier)
    if args.write:
        (pilot_dir / "report.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def pilot(args) -> int:
    sys.path.insert(0, str(ROOT))
    from pipeline.config import Settings
    from pipeline.pilot import run_pilot

    settings = Settings.load(args.config)
    if settings.provider != "fake" and not args.paid:
        print("This would call a paid API. Add --paid to confirm.")
        return 2
    out = args.out or (HERE if settings.provider != "fake" else ROOT / "runs" / f"{args.run}-synthetic-cost")
    result = run_pilot(args.input, out, args.run, args.config, {})
    print(f"Pilot saved to {out}: cold {result['cold']['wall_seconds']} s, warm {result['warm']['wall_seconds']} s, "
          f"{result['warm']['new_enrichment_calls']} new enrichment calls on the warm run.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pilot-dir", type=Path, default=HERE, help="folder with pilot_runs.json and usage.csv")
    parser.add_argument("--rates", type=Path, default=HERE / "rates.csv")
    parser.add_argument("--rate-multiplier", type=float, default=1.0)
    parser.add_argument("--write", action="store_true", help="also save report.md next to the pilot files")
    for key, value in DEFAULTS.items():
        parser.add_argument(f"--{key.replace('_', '-')}", dest=key, type=type(value), default=value)
    parser.set_defaults(handler=replay)
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("pilot", help="run the real cold/warm pilot (explicit; paid unless the provider is fake)")
    p.add_argument("--paid", action="store_true")
    p.add_argument("--config", type=Path, help="TOML settings; omit to use the offline fake provider")
    p.add_argument("--input", type=Path, default=ROOT / "data" / "cost_100.csv")
    p.add_argument("--run", required=True)
    p.add_argument("--out", type=Path)
    p.set_defaults(handler=pilot)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
