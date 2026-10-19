"""The measured pilot: run the configured chain cold, then warm, and save what the calculator replays."""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import shutil
import time
from pathlib import Path

from . import cli
from .config import RUNS
from .db import connect, meta_get
from .rank import read_jsonl

STAGES = (("prepare", cli.cmd_prepare), ("enrich", cli.cmd_enrich), ("fallback", cli.cmd_fallback),
          ("verify", cli.cmd_verify), ("finalize", cli.cmd_finalize), ("group", cli.cmd_group),
          ("diagnose", cli.cmd_diagnose), ("memo", cli.cmd_memo))


def _pass(args) -> dict:
    """One end-to-end pass. Wall-clock is measured with a clock, not summed from call durations."""
    stage_seconds, started = {}, time.perf_counter()
    for name, command in STAGES:
        tick = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):
            code = command(args)
        stage_seconds[name] = round(time.perf_counter() - tick, 4)
        if code:
            raise SystemExit(f"Pilot stage '{name}' stopped with code {code}; see `python -m pipeline status --run {args.run}`")
    return {"wall_seconds": round(time.perf_counter() - started, 4), "stage_seconds": stage_seconds}


def run_pilot(input_csv: Path, out: Path, run: str, config: Path | None, overrides: dict) -> dict:
    run_dir = RUNS / run
    if run_dir.exists():
        raise SystemExit(f"{run_dir} exists. A cold pilot needs an empty result cache: pick a new --run id.")
    args = argparse.Namespace(**{"symptoms": None, "reset": False, **overrides}, run=run, input=Path(input_csv),
                              config=config, max_requests=None, fake_interrupt_after=None)
    cold = _pass(args)
    conn = connect(run_dir / "pipeline.sqlite")
    cold_invocations = int(meta_get(conn, "invocation"))
    args.config = None  # the warm pass reuses the settings saved by the cold one
    warm = _pass(args)

    out.mkdir(parents=True, exist_ok=True)
    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    settings = summary["settings"]
    usage_rows, calls = [], {"cold": 0, "warm": 0}
    with (out / "pilot_calls.jsonl").open("w", encoding="utf-8") as f:
        for c in conn.execute("SELECT * FROM calls ORDER BY started_at, request_id"):
            phase = "cold" if c["invocation"] <= cold_invocations else "warm"
            calls[phase] += 1
            ids = json.loads(c["review_ids"])
            f.write(json.dumps({
                "run": f"{run}:{phase}", "request_id": c["request_id"], "role": c["role"], "step": c["step"],
                "provider": c["provider"], "model": c["model"], "effort": c["effort"], "tier": c["tier"],
                "label_config": c["label_config"], "outcome": c["outcome"], "error": c["error"],
                "review_ids": ids, "batch_size": len(ids), "input_tokens": c["input_tokens"],
                "cached_input_tokens": c["cached_input_tokens"], "cache_write_tokens": c["cache_write_tokens"],
                "output_tokens": c["output_tokens"], "stop_reason": c["stop_reason"],
                "reasoning_tokens": c["reasoning_tokens"], "usage_available": bool(c["usage_available"]),
                "started_at": c["started_at"], "duration_ms": c["duration_ms"]}, ensure_ascii=False) + "\n")
            uncached = c["input_tokens"] - c["cached_input_tokens"] - c["cache_write_tokens"]
            for item, units in (("input", uncached), ("cache_write", c["cache_write_tokens"]),
                                ("cached_input", c["cached_input_tokens"]), ("output", c["output_tokens"])):
                usage_rows.append([phase, c["request_id"], c["role"], c["step"], c["provider"], c["model"], c["tier"],
                                   item, units, "tokens", int(c["usage_available"]), len(ids)])
    with (out / "usage.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["run", "request_id", "role", "step", "provider", "model", "tier", "item", "billed_units",
                         "unit", "usage_available", "texts_in_request"])
        writer.writerows(usage_rows)
    with (out / "pilot_records.jsonl").open("w", encoding="utf-8") as f:
        for record in read_jsonl(run_dir / "records.jsonl.gz"):
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    shutil.copyfile(run_dir / "ranking.csv", out / "pilot_ranking.csv")

    for phase, n in (cold, calls["cold"]), (warm, calls["warm"]):
        phase["model_calls"] = n
    warm["new_enrichment_calls"] = conn.execute(
        "SELECT COUNT(*) FROM calls WHERE role='enrich' AND invocation>?", (cold_invocations,)).fetchone()[0]
    warm["new_diagnose_calls"] = conn.execute(
        "SELECT COUNT(*) FROM calls WHERE step='diagnose' AND invocation>?", (cold_invocations,)).fetchone()[0]
    diagnoses = dict(conn.execute("SELECT status, COUNT(*) FROM diagnoses GROUP BY status").fetchall())
    pilot = {
        "synthetic": settings["provider"] == "fake",
        "input_file": Path(input_csv).name, "input_sha256": summary["input_sha256"], "run": run,
        "label_config": summary["label_config"], "settings": settings,
        "records": summary["source_rows"], "completed": summary["completed"], "failed": summary["quarantined"],
        "unique_texts": summary["unique_texts"], "result_cache_hits": summary["cache_reuse"],
        "fallback_texts": summary["fallback_texts"],
        "mean_chars_per_unique_text": conn.execute("SELECT AVG(n_chars) FROM texts").fetchone()[0],
        # the diagnose stage scales with diagnosable texts (complaints in the four areas, not a free-tier limit)
        "diagnosable_texts": sum(diagnoses.values()), "diagnosed_texts": diagnoses.get("completed", 0),
        "stages_included": [name for name, _ in STAGES if name != "diagnose" or diagnoses],
        "stages_not_yet_included": [] if diagnoses else ["diagnose (no approved symptom catalog)"],
        "cold": cold, "warm": warm, "measured_at": time.time(),
    }
    (out / "pilot_runs.json").write_text(json.dumps(pilot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return pilot
