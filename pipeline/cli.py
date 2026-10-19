"""Command line: each stage is a command that reads the previous stage's saved output.

    python -m pipeline run --input data/cost_100.csv --run demo100
    python -m pipeline rank --run demo100        # ranking from saved files, no model call
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields, replace
from pathlib import Path

from .budget import Rates, reserved, spent
from .config import RUNS, Settings, label_config, load_taxonomy
from .db import backlog_counts, connect, diagnosis_counts, meta_get, meta_set
from .diagnose import (Diagnoser, bind_catalog, bound_catalog, fill_backlog, label_config as diagnose_config,
                       load_symptoms, release_stale_diagnoses)
from .enrich import Enricher, release_stale, release_unfunded_fallback, resolve_fallback_cap
from .export import export_grading, finalize, rerank, write_checkpoint
from .group import approve_symptoms, approve_taxonomy, discover_subtopics, discover_symptoms, name_issues
from .memo import run_memo
from .prepare import prepare
from .providers import get_provider
from .roles import Writer
from .symptom_tables import write_diagnosis
from .verify import run_diagnosis_check, run_verify

OVERRIDES = ("provider", "model", "mode", "batch_size", "budget_usd", "poll_seconds", "symptoms")


def _say(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, (dict, list)) else value)


def _open(args):
    run_dir = RUNS / args.run
    return run_dir, connect(run_dir / "pipeline.sqlite")


def _settings(conn, args) -> Settings:
    """Settings are fixed by the first enrichment call of a run and reloaded afterwards."""
    saved = meta_get(conn, "settings")
    overrides = {name: getattr(args, name, None) for name in OVERRIDES}
    if saved and not getattr(args, "config", None):
        known = {f.name for f in fields(Settings)}
        base = {k: v for k, v in json.loads(saved).items() if k in known}
        return Settings.load(None, **{**base, **{k: v for k, v in overrides.items() if v is not None}})
    return Settings.load(getattr(args, "config", None), **overrides)


def _begin(conn, settings: Settings, tax: dict) -> int:
    config = label_config(settings, tax)
    saved = meta_get(conn, "label_config")
    if saved and saved != config:
        raise SystemExit(f"This run was labelled under\n  {saved}\nbut the current configuration is\n  {config}\n"
                         "Results are only reused under an unchanged configuration: start a new --run id.")
    invocation = int(meta_get(conn, "invocation", "0")) + 1
    meta_set(conn, "invocation", str(invocation))
    meta_set(conn, "label_config", config)
    meta_set(conn, "settings", json.dumps(settings.as_dict()))
    return invocation


def _work(args, step: str) -> int:
    run_dir, conn = _open(args)
    if not meta_get(conn, "input_sha256"):
        raise SystemExit(f"Run {args.run} has no input yet: run `prepare --input <csv> --run {args.run}` first.")
    settings = _settings(conn, args)
    tax = load_taxonomy(settings)
    released = release_stale(conn)
    cap = resolve_fallback_cap(conn, settings) if step == "fallback" else None
    invocation = _begin(conn, settings, tax)
    fake = {"interrupt_after": args.fake_interrupt_after} if settings.provider == "fake" else {}
    provider = get_provider(settings.provider, settings.model, run_dir, **fake)
    enricher = Enricher(conn, provider, settings, Rates(), tax, invocation, max_requests=args.max_requests)
    try:
        stopped = enricher.run(step) or "finished"
    except KeyboardInterrupt:
        release_stale(conn)  # the request in flight was never saved; its texts go back to the backlog
        stopped = "interrupted"
    except Exception as error:  # any other crash still leaves a checkpoint of what was saved, then surfaces
        release_stale(conn)
        write_checkpoint(conn, run_dir, invocation, f"crashed: {type(error).__name__}")
        raise
    if step == "fallback" and stopped.startswith("budget"):
        release_unfunded_fallback(conn)
    checkpoint = write_checkpoint(conn, run_dir, invocation, stopped)
    _say({"step": step, "invocation": invocation, "ended": stopped, "requests": enricher.requests_made,
          "stale_leases_released": released, "fallback_cap": cap, "backlog": backlog_counts(conn),
          "spent_usd": round(spent(conn), 6), "checkpoint": str(checkpoint.relative_to(run_dir.parent.parent))})
    return 130 if stopped == "interrupted" else 0


def _role(args):
    """Open a run for one of the model roles after enrichment (verify, group, memo)."""
    run_dir, conn = _open(args)
    settings = _settings(conn, args)
    tax = load_taxonomy(settings)
    return run_dir, conn, settings, tax, _begin(conn, settings, tax)


def _writer(conn, run_dir, settings, invocation) -> Writer:
    return Writer(conn, get_provider(settings.provider, settings.model, run_dir), settings, Rates(), invocation)


def cmd_verify(args) -> int:
    run_dir, conn, settings, tax, invocation = _role(args)
    report = run_verify(conn, settings, run_dir, invocation)
    _say({k: report[k] for k in ("n", "agreement", "severity_mae", "calls_this_run", "stopped", "verifier")})
    return 1 if report["stopped"] else 0


def cmd_group(args) -> int:
    run_dir, conn, settings, tax, invocation = _role(args)
    result = name_issues(conn, run_dir, _writer(conn, run_dir, settings, invocation), tax,
                         settings.group_examples_per_issue)
    _say({"issues_named": len(result["issues"]), "problems": result["problems"], "from_cache": result["from_cache"]})
    return 0


def cmd_discover(args) -> int:
    run_dir, conn, settings, tax, invocation = _role(args)
    result = discover_subtopics(conn, run_dir, _writer(conn, run_dir, settings, invocation), tax,
                                settings.discover_examples_per_topic)
    _say({topic: [s["id"] for s in subs] for topic, subs in result["proposal"]["topics"].items()})
    _say({"problems": result["problems"], "proposal": str(run_dir / "taxonomy_proposal.json"),
          "next": f"review it, then: python -m pipeline approve-taxonomy --run {args.run}"})
    return 0


def cmd_approve_taxonomy(args) -> int:
    path = approve_taxonomy(RUNS / args.run)
    _say(f"Approved taxonomy written to {path}. It changes the label_config, so enrichment needs a new --run id.")
    return 0


def _has_records(conn) -> bool:
    return bool(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='records'").fetchone()[0])


def cmd_discover_symptoms(args) -> int:
    run_dir, conn = _open(args)
    if not _has_records(conn):
        raise SystemExit(f"Run {args.run} is not finalized yet: run `finalize --run {args.run}` first.")
    run_dir, conn, settings, tax, invocation = _role(args)
    result = discover_symptoms(conn, run_dir, _writer(conn, run_dir, settings, invocation), tax,
                               settings.symptom_examples)
    _say({area: [s["id"] for s in symptoms] for area, symptoms in result["proposal"]["areas"].items()})
    _say({"problems": result["problems"], "proposal": str(run_dir / "symptoms_proposal.json"),
          "next": f"review it, then: python -m pipeline approve-symptoms --run {args.run}"})
    return 0


def cmd_approve_symptoms(args) -> int:
    path = approve_symptoms(RUNS / args.run, args.out)
    _say(f"Approved symptom catalog written to {path}. `diagnose` now applies it.")
    return 0


def cmd_diagnose(args) -> int:
    """Stage 4b: every diagnosable complaint text gets a symptom, context, trigger and long-time-user flag."""
    run_dir, conn = _open(args)
    settings = _settings(conn, args)
    catalog = load_symptoms(settings)
    if catalog is None:
        _say("diagnose skipped: no approved symptom catalog. Run `discover-symptoms --run <run>`, review "
             "symptoms_proposal.json, then `approve-symptoms --run <run>`.")
        return 0
    if not meta_get(conn, "input_sha256") or not _has_records(conn):
        raise SystemExit(f"Run {args.run} is not finalized yet: run enrichment and `finalize --run {args.run}` first.")
    tax = load_taxonomy(settings)
    invocation = _begin(conn, settings, tax)
    bind_catalog(conn, catalog, diagnose_config(settings, catalog), reset=getattr(args, "reset", False))
    released = release_stale_diagnoses(conn)
    added = fill_backlog(conn)
    fake = {"interrupt_after": args.fake_interrupt_after} if settings.provider == "fake" else {}
    provider = get_provider(settings.provider, settings.model, run_dir, **fake)
    diagnoser = Diagnoser(conn, provider, settings, Rates(), catalog, invocation, max_requests=args.max_requests)
    try:
        stopped = diagnoser.run() or "finished"
    except KeyboardInterrupt:
        release_stale_diagnoses(conn)  # the request in flight was never saved; its texts go back to the backlog
        stopped = "interrupted"
    except Exception:
        release_stale_diagnoses(conn)
        raise
    counts = diagnosis_counts(conn)
    check = None
    if stopped == "finished" and not counts.get("pending") and not counts.get("leased") and counts.get("completed"):
        report = run_diagnosis_check(conn, settings, run_dir, invocation, catalog)
        check = {k: report[k] for k in ("n", "agreement", "disagreements_total", "stopped", "verifier")}
    tables = write_diagnosis(conn, run_dir) if counts.get("completed") else None
    _say({"step": "diagnose", "invocation": invocation, "ended": stopped, "requests": diagnoser.requests_made,
          "stale_requests_released": released, "added_to_backlog": added, "diagnoses": counts,
          "check": check, "tables": "written" if tables else "not written (no completed diagnosis or no records)",
          "spent_usd": round(spent(conn), 6), "diagnose_spent_usd": round(spent(conn, "diagnose"), 6)})
    return 130 if stopped == "interrupted" else 0


def cmd_verify_diagnosis(args) -> int:
    run_dir, conn = _open(args)
    catalog = bound_catalog(conn)
    if catalog is None or not diagnosis_counts(conn).get("completed"):
        _say("verify-diagnosis skipped: this run has no completed diagnosis yet; run `diagnose` first.")
        return 0
    settings = _settings(conn, args)
    invocation = _begin(conn, settings, load_taxonomy(settings))
    report = run_diagnosis_check(conn, settings, run_dir, invocation, catalog)
    _say({k: report[k] for k in ("n", "agreement", "by_area", "disagreements_total", "calls_this_run", "stopped",
                                 "verifier")})
    return 1 if report["stopped"] else 0


def cmd_memo(args) -> int:
    run_dir, conn, settings, tax, invocation = _role(args)
    # The memo writer and critic think harder than the other writer roles: many constraints, four calls.
    memo_settings = replace(settings, writer_effort=settings.memo_effort)
    _say(run_memo(conn, run_dir, _writer(conn, run_dir, memo_settings, invocation), settings))
    return 0


def cmd_prepare(args) -> int:
    run_dir, conn = _open(args)
    _say(prepare(conn, args.input, run_dir))
    return 0


def cmd_enrich(args) -> int:
    return _work(args, "primary")


def cmd_fallback(args) -> int:
    return _work(args, "fallback")


def cmd_finalize(args) -> int:
    run_dir, conn = _open(args)
    _say(finalize(conn, run_dir, load_taxonomy(_settings(conn, args))))
    return 0


def cmd_rank(args) -> int:
    rows = rerank(RUNS / args.run)
    _say(f"ranking.csv regenerated from saved records and membership: {len(rows)} issues, 0 model calls")
    for row in rows[:10]:
        _say(f"  {row['rank']:>2}  {row['issue_id']:<32} n={row['complaint_count']:<7} "
             f"mean={row['mean_severity']}  score={row['priority_score']}")
    if (RUNS / args.run / "pipeline.sqlite").exists():
        _, conn = _open(args)
        if write_diagnosis(conn, RUNS / args.run):
            _say("diagnosis tables regenerated from saved records and diagnoses, 0 model calls")
    return 0


def cmd_export(args) -> int:
    run_dir, conn = _open(args)
    _say(export_grading(conn, run_dir, args.out, args.before_invocation))
    return 0


def cmd_status(args) -> int:
    run_dir, conn = _open(args)
    _say({"run": args.run, "label_config": meta_get(conn, "label_config"),
          "invocations": int(meta_get(conn, "invocation", "0")), "backlog": backlog_counts(conn),
          "diagnoses": diagnosis_counts(conn),
          "reviews": conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
          "calls": conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0],
          "failed_calls": conn.execute("SELECT COUNT(*) FROM calls WHERE outcome='failed'").fetchone()[0],
          "batches_running": conn.execute(
              "SELECT COUNT(*) FROM provider_batches WHERE status='in_progress'").fetchone()[0],
          "spent_usd": round(spent(conn), 6), "reserved_usd": round(reserved(conn), 6)})
    return 0


def cmd_run(args) -> int:
    """The chain, in order. Each step is idempotent, so rerunning the command resumes the run."""
    for step in (cmd_prepare, cmd_enrich, cmd_fallback, cmd_verify, cmd_finalize, cmd_group, cmd_diagnose, cmd_memo):
        code = step(args)
        if code:
            return code
        if step is cmd_enrich:
            _, conn = _open(args)
            if backlog_counts(conn).get("pending", 0) or backlog_counts(conn).get("leased", 0):
                _say("Enrichment stopped with work left in the backlog; rerun the same command to resume.")
                return 1
        if step is cmd_diagnose:
            _, conn = _open(args)
            if diagnosis_counts(conn).get("pending", 0) or diagnosis_counts(conn).get("leased", 0):
                _say("Diagnosis stopped with work left in its backlog; rerun the same command to resume.")
                return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, help_text, *, work=False, needs_input=False):
        p = commands.add_parser(name, help=help_text)
        p.add_argument("--run", required=True, help="run id; state lives in runs/<run>/")
        p.set_defaults(handler=handler, max_requests=None, fake_interrupt_after=None, config=None, symptoms=None,
                       reset=False)
        if needs_input:
            p.add_argument("--input", required=True, type=Path, help="input CSV path")
        if work:
            p.add_argument("--config", type=Path, help="TOML settings file (first call of a run)")
            p.add_argument("--provider")
            p.add_argument("--model")
            p.add_argument("--mode", choices=("sync", "batch"))
            p.add_argument("--batch-size", dest="batch_size", type=int)
            p.add_argument("--budget-usd", dest="budget_usd", type=float)
            p.add_argument("--poll-seconds", dest="poll_seconds", type=float)
            p.add_argument("--max-requests", dest="max_requests", type=int,
                           help="stop admitting work after this many requests in this invocation")
            p.add_argument("--fake-interrupt-after", dest="fake_interrupt_after", type=int,
                           help="fake provider only: simulate Ctrl-C after N requests")
        return p

    add("prepare", cmd_prepare, "stage 1: ingest, hash, dedup, build the backlog", needs_input=True)
    add("enrich", cmd_enrich, "stage 2: label the backlog in requests of at most 50", work=True)
    add("fallback", cmd_fallback, "stage 2b: re-ask doubted texts one by one, within the cap", work=True)
    add("verify", cmd_verify, "stage 3: a different model re-labels a declared sample, blind", work=True)
    add("finalize", cmd_finalize, "join results to every source id; membership, ranking, trends")
    add("group", cmd_group, "stage 4: name the ranked issues from bounded evidence", work=True)
    add("discover", cmd_discover, "phase A: propose subtopics from this run's sample", work=True)
    add("approve-taxonomy", cmd_approve_taxonomy, "human gate: adopt this run's subtopic proposal")
    symptoms_help = "symptom catalog (default prompts/symptoms.json; a path outside prompts/ must be absolute)"
    add("discover-symptoms", cmd_discover_symptoms, "calibration: propose concrete symptoms per area", work=True)
    approve = add("approve-symptoms", cmd_approve_symptoms, "human gate: adopt this run's symptom proposal")
    approve.add_argument("--out", type=Path, help="write the catalog here instead of prompts/symptoms.json")
    diagnose = add("diagnose", cmd_diagnose, "stage 4b: symptom, context, trigger per diagnosable complaint",
                   work=True)
    diagnose.add_argument("--symptoms", help=symptoms_help)
    diagnose.add_argument("--reset", action="store_true",
                          help="discard this run's diagnoses (e.g. after a catalog change) and start again")
    add("verify-diagnosis", cmd_verify_diagnosis, "blind check of a declared sample of diagnoses", work=True)
    add("memo", cmd_memo, "stage 6: memo writer, code checks, critic, at most one revision", work=True)
    add("rank", cmd_rank, "regenerate ranking.csv from saved outputs, no model call")
    export = add("export", cmd_export, "write the grading/ folder")
    export.add_argument("--out", type=Path, default=Path("grading"))
    export.add_argument("--before-invocation", dest="before_invocation", type=int, default=1,
                        help="the invocation that was interrupted; later ones are the resume phase")
    add("status", cmd_status, "backlog, calls and spend")
    run = add("run", cmd_run, "prepare -> enrich -> fallback -> verify -> finalize -> group -> diagnose -> memo",
              work=True, needs_input=True)
    run.add_argument("--symptoms", help=symptoms_help)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
