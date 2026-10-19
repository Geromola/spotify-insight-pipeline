"""Model-selection probe: Claude Haiku 5.5 vs Jev vs OpenAI's Decisions API on the same 50 reviews.

Measures real billed tokens, cost per review, projected corpus cost, latency, and how often the
models agree. Development data only (rows 100-149 of checkpoint_500.csv); never the golden 50.

    .venv/bin/python evals/classifier_probe.py run             # paid, a few cents; writes evals/classifier_probe/
    .venv/bin/python evals/classifier_probe.py run decisions   # repeat only the OpenAI arms, keep the rest
    .venv/bin/python evals/classifier_probe.py summarize       # offline: rebuilds summary.md from responses.jsonl
"""
from __future__ import annotations

import csv
import json
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.budget import Rates  # noqa: E402
from pipeline.config import Settings, load_taxonomy  # noqa: E402
from pipeline.enrich import Item, parse, system_prompt  # noqa: E402
from pipeline.textproc import render, segments  # noqa: E402

OUT = ROOT / "evals" / "classifier_probe"
SAMPLE = slice(100, 150)        # dev rows outside the 100-review pilot
WARM_SAMPLE = slice(150, 200)   # a second Haiku request, to measure the cached steady state
UNIQUE_TEXTS = 484_189
FIELDS = ("topic", "intent", "severity")


def dev_rows(rows: slice) -> list[dict]:
    with (ROOT / "data" / "checkpoint_500.csv").open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))[rows]


def log(kind: str, record: dict) -> None:
    with (OUT / f"{kind}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── Claude Haiku 5.5: the pipeline's own prompt and parser ─────────────────────
def haiku_arm(arm: str, rows: list[dict], tax: dict, settings: Settings) -> None:
    from pipeline.providers.anthropic_haiku import AnthropicProvider
    from pipeline.providers.base import ModelRequest

    items = [Item(i, "", r["review_text"], r["review_id"], segments(r["review_text"], settings.quote_full_max))
             for i, r in enumerate(rows)]
    user = "\n".join(render(it.idx, it.segs) for it in items)
    request = ModelRequest(f"probe-{uuid.uuid4().hex[:10]}", "enrich", "primary", system_prompt("primary", tax),
                           user, settings.effort, 20 * len(items) + 40, settings.thinking)
    log("requests", {"arm": arm, "provider": "anthropic", "system": request.system, "user": user})
    resp = AnthropicProvider().complete(request)
    good, bad = parse(resp.text, items, tax, resp.stop_reason, resp.refusal_category) if resp.ok else ({}, {})
    labels = {items[i].origin_id: {k: rec[k] for k in (*FIELDS, "sentiment")} for i, rec in good.items()}
    log("responses", {"arm": arm, "provider": "anthropic", "model": "claude-haiku-5-5", "tier": "standard",
                      "ok": resp.ok, "error": resp.error, "n_reviews": len(rows), "labels": labels,
                      "invalid": len(bad), "input_tokens": resp.input_tokens,
                      "cached_input_tokens": resp.cached_input_tokens, "cache_write_tokens": resp.cache_write_tokens,
                      "output_tokens": resp.output_tokens, "duration_ms": resp.duration_ms, "request_id": resp.request_id})


# ── Jev ─────────────────────────────────────────────────────────────────────
def jev_single_arm(rows: list[dict], tax: dict, arm: str = "jev_single") -> None:
    from pipeline.providers.typesafe_jev import JevClassifier
    jev = JevClassifier()
    for r in rows:
        out = jev.classify(r["review_text"], segments(r["review_text"], 80), tax)
        labels = {r["review_id"]: {k: out["record"][k] for k in (*FIELDS, "sentiment")}} if out["ok"] else {}
        log("responses", {"arm": arm, "provider": "typesafe", "model": out.get("model", "jev-1.13.0"),
                          "tier": "standard", "ok": out["ok"], "error": out.get("error"), "n_reviews": 1,
                          "labels": labels, "input_tokens": out["input_tokens"], "cached_input_tokens": 0,
                          "cache_write_tokens": 0, "output_tokens": out["output_tokens"],
                          "duration_ms": out["duration_ms"], "request_id": out["request_id"]})


def rubric() -> dict:
    from pipeline.providers.typesafe_jev import INTENTS, SEVERITY, TOPICS
    return {"topics": TOPICS, "intents": INTENTS, "severity": SEVERITY,
            "rules": "Topic: the most severe problem, the first on a tie; for praise, the first praised feature. "
                     "Intent precedence: cancellation, complaint, request, praise, unclear. "
                     "Severity is the reported impact, not anger or stars."}


def jev_packed_arm(rows: list[dict], arm: str) -> None:
    """Definitions sent once in the state, then four minimal questions per review."""
    from typesafe_sdk import Choice, Score

    from pipeline.providers.typesafe_jev import INTENTS, SENTIMENT, SEVERITY, TOPICS, JevClassifier
    jev = JevClassifier()
    state = {"rubric": rubric(), "reviews": {f"r{i}": r["review_text"] for i, r in enumerate(rows)}}
    questions = {}
    for i in range(len(rows)):
        questions[f"r{i}_topic"] = Choice(instructions=f"Topic of `reviews.r{i}`, as defined in `rubric.topics`.",
                                          criteria={name: None for name in TOPICS})
        questions[f"r{i}_intent"] = Choice(instructions=f"Intent of `reviews.r{i}`, as defined in `rubric.intents`.",
                                           criteria={name: None for name in INTENTS})
        questions[f"r{i}_severity"] = Choice(instructions=f"Severity of `reviews.r{i}`, as defined in `rubric.severity`.",
                                             criteria={name: None for name in SEVERITY})
        questions[f"r{i}_sentiment"] = Score(instructions=f"Overall tone of `reviews.r{i}`.", criteria=SENTIMENT)
    log("requests", {"arm": arm, "provider": "typesafe", "state": state, "questions": len(questions)})
    started = time.time()
    try:
        response = jev.client.system_one(state=state, questions=questions)
    except Exception as e:  # the probe records failures instead of stopping
        log("responses", {"arm": arm, "provider": "typesafe", "model": "jev-1.13.0", "tier": "standard", "ok": False,
                          "error": f"{type(e).__name__}: {e}"[:300], "n_reviews": len(rows), "labels": {},
                          "input_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0,
                          "duration_ms": int((time.time() - started) * 1000), "request_id": None})
        return
    c, s = response.choices, response.scores
    labels = {r["review_id"]: {"topic": c[f"r{i}_topic"].choice, "intent": c[f"r{i}_intent"].choice,
                               "severity": int(c[f"r{i}_severity"].choice),
                               "sentiment": round(s[f"r{i}_sentiment"].score / 2 - 1, 3)} for i, r in enumerate(rows)}
    log("responses", {"arm": arm, "provider": "typesafe", "model": response.model, "tier": "standard", "ok": True,
                      "error": None, "n_reviews": len(rows), "labels": labels,
                      "input_tokens": response.usage.input_tokens or 0, "cached_input_tokens": 0,
                      "cache_write_tokens": 0, "output_tokens": response.usage.output_tokens or 0,
                      "duration_ms": int((time.time() - started) * 1000), "request_id": response.request_id})


# ── OpenAI Decisions API ─────────────────────────────────────────────────────
def decisions_client():
    from openai import OpenAI

    from pipeline.env import require
    require("OPENAI_API_KEY")
    return OpenAI(max_retries=0, timeout=60.0)


def _choice(name: str, instructions: str, options: dict, described: bool) -> dict:
    return {"type": "choice", "name": name, "instructions": instructions,
            "choices": [{"value": v, **({"description": d} if described else {})} for v, d in options.items()]}


def _decide(client, arm: str, text: str, questions: list[dict], ids: list[str], parse_answers) -> None:
    log("requests", {"arm": arm, "provider": "openai", "input": text, "questions": questions})
    started = time.time()
    try:
        decision = client.decisions.create(model="gpt-6-luna", input=text, questions=questions)
    except Exception as e:
        log("responses", {"arm": arm, "provider": "openai", "model": "gpt-6-luna", "tier": "decisions", "ok": False,
                          "error": f"{type(e).__name__}: {e}"[:300], "n_reviews": len(ids), "labels": {},
                          "input_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0,
                          "duration_ms": int((time.time() - started) * 1000), "request_id": None})
        return
    answers = {a.name: a for a in decision.answers}
    u = decision.usage
    log("responses", {"arm": arm, "provider": "openai", "model": decision.model, "tier": "decisions", "ok": True,
                      "error": None, "n_reviews": len(ids), "labels": parse_answers(answers),
                      "refusals": sum(a.type == "refusal" for a in decision.answers),
                      "input_tokens": u.input_tokens, "cached_input_tokens": u.input_tokens_details.cached_tokens,
                      "cache_write_tokens": u.input_tokens_details.cache_write_tokens, "output_tokens": u.output_tokens,
                      "duration_ms": int((time.time() - started) * 1000), "request_id": getattr(decision, "_request_id", None)})


def _labels(answers: dict, prefix: str) -> dict | None:
    try:
        return {"topic": answers[f"{prefix}topic"].choice, "intent": answers[f"{prefix}intent"].choice,
                "severity": int(answers[f"{prefix}severity"].choice),
                "sentiment": round(answers[f"{prefix}sentiment"].score / 2 - 1, 3)}
    except (KeyError, AttributeError):  # a refusal answer has no choice
        return None


def decisions_single_arm(client, rows: list[dict], arm: str = "decisions_single") -> None:
    from pipeline.providers.typesafe_jev import INTENTS, SENTIMENT, SEVERITY, TOPICS
    for r in rows:
        questions = [
            _choice("topic", "Which product area is this Spotify app review mainly about? With several problems, "
                             "the most severe one; for praise, the first praised feature.", TOPICS, True),
            _choice("intent", "What is the reviewer doing?", INTENTS, True),
            _choice("severity", "How severe is the impact the review actually reports? Judge the stated impact, "
                                "not the anger or the star rating.", SEVERITY, True),
            {"type": "score", "name": "sentiment", "instructions": "Overall tone of the review.",
             "levels": [{"label": level} for level in SENTIMENT]},
        ]
        _decide(client, arm, r["review_text"], questions, [r["review_id"]],
                lambda a, rid=r["review_id"]: {rid: lab} if (lab := _labels(a, "")) else {})


def decisions_packed_arm(client, rows: list[dict], arm: str) -> None:
    from pipeline.providers.typesafe_jev import INTENTS, SENTIMENT, SEVERITY, TOPICS
    text = "RUBRIC\n" + json.dumps(rubric(), ensure_ascii=False) + "\n\nREVIEWS\n" + "\n".join(
        f"r{i}: {' '.join(r['review_text'].split())}" for i, r in enumerate(rows))
    questions = []
    for i in range(len(rows)):  # 4 questions per review; the API allows 200 per request
        questions += [
            _choice(f"r{i}_topic", f"Topic of review r{i}, as defined in the rubric.", TOPICS, False),
            _choice(f"r{i}_intent", f"Intent of review r{i}, as defined in the rubric.", INTENTS, False),
            _choice(f"r{i}_severity", f"Severity of review r{i}, as defined in the rubric.", SEVERITY, False),
            {"type": "score", "name": f"r{i}_sentiment", "instructions": f"Overall tone of review r{i}.",
             "levels": [{"label": level} for level in SENTIMENT]},
        ]
    ids = [r["review_id"] for r in rows]
    _decide(client, arm, text, questions, ids,
            lambda a: {rid: lab for i, rid in enumerate(ids) if (lab := _labels(a, f"r{i}_"))})


# ── summary (offline) ───────────────────────────────────────────────────────
def summarize() -> str:
    rates = Rates(ROOT / "cost" / "rates.csv")
    responses = [json.loads(line) for line in (OUT / "responses.jsonl").open(encoding="utf-8")]
    model_of = {"anthropic": "claude-haiku-5-5", "typesafe": "jev-1.13.0", "openai": "gpt-6-luna"}
    arms: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    labels: dict[str, dict] = defaultdict(dict)
    for r in responses:
        a = arms[r["arm"]]
        a["calls"] += 1
        a["failed"] += not r["ok"]
        a["reviews"] += len(r["labels"])
        a["sent"] += r["n_reviews"]
        a["input"] += r["input_tokens"]
        a["cached"] += r["cached_input_tokens"]
        a["output"] += r["output_tokens"]
        a["ms"] += r["duration_ms"] or 0
        a["cost"] += rates.cost(r["provider"], model_of[r["provider"]], r["tier"], r["input_tokens"],
                                r["cached_input_tokens"], r["output_tokens"], r["cache_write_tokens"])
        if r["provider"] == "anthropic":
            a["cost_batch"] += rates.cost("anthropic", "claude-haiku-5-5", "batch", r["input_tokens"],
                                          r["cached_input_tokens"], r["output_tokens"], r["cache_write_tokens"])
        a["provider"] = r["provider"]
        a.setdefault("error", r["error"])
        labels[r["arm"]].update(r["labels"])

    def agree(x: str, y: str) -> str:
        common = labels[x].keys() & labels[y].keys()
        if not common:
            return "—"
        return " / ".join(f"{sum(labels[x][k][f] == labels[y][k][f] for k in common) / len(common):.0%}" for f in FIELDS)

    lines = ["| Brazo | Llamadas (fallidas) | Reseñas etiquetadas | Tokens de entrada por reseña | Caché leído | "
             "Salida por reseña | Costo por reseña | Proyección 484,189 textos | Latencia media | "
             "Acuerdo con Haiku (topic / intent / severidad) |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    order = ["haiku_cold", "haiku_warm", "jev_single", "jev_packed_10", "jev_packed_25", "jev_packed_50",
             "decisions_single", "decisions_packed_10", "decisions_packed_50"]
    for name in [n for n in order if n in arms] + [n for n in arms if n not in order and not n.startswith("cache_")]:
        a = arms[name]
        if not a["reviews"]:  # never print a measured-looking zero for an arm that did not run
            lines.append(f"| {name} | {int(a['calls'])} ({int(a['failed'])}) | 0 | no medido: {a['error'][:90]} "
                         "| | | | | | |")
            continue
        per = a["cost"] / a["sent"] if a["sent"] else 0
        projection = f"${per * UNIQUE_TEXTS:,.2f}"
        if a["provider"] == "anthropic":
            projection += f" (lote: ${a['cost_batch'] / a['sent'] * UNIQUE_TEXTS:,.2f})"
        lines.append(f"| {name} | {int(a['calls'])} ({int(a['failed'])}) | {int(a['reviews'])} | "
                     f"{a['input'] / a['sent']:.0f} | {a['cached'] / a['sent']:.0f} | {a['output'] / a['sent']:.0f} | "
                     f"${per:.7f} | {projection} | {a['ms'] / a['calls']:.0f} ms | "
                     f"{agree(name, 'haiku_cold') if not name.startswith('haiku') else '—'} |")
    lines += ["", "Empaque contra individual (topic / intent / severidad): "
              f"Jev 50 vs Jev individual {agree('jev_packed_50', 'jev_single')}; "
              f"Decisions 50 vs Decisions individual {agree('decisions_packed_50', 'decisions_single')}; "
              f"Jev individual vs Decisions individual {agree('jev_single', 'decisions_single')}.", ""]
    for name in sorted(n for n in arms if n.startswith("cache_")):
        runs = [r for r in responses if r["arm"] == name and r["ok"]]
        if not runs:
            lines.append(f"Caché oculto, {name}: no medido ({arms[name]['error'][:90]}).")
            continue
        lines.append(f"Caché oculto, {name}: tokens de entrada por repetición "
                     f"{[r['input_tokens'] for r in runs]}, leídos de caché {[r['cached_input_tokens'] for r in runs]}.")
    total = sum(a["cost"] for a in arms.values())
    lines += ["", f"Gasto total de la prueba: ${total:.4f} (tarifas de `cost/rates.csv`, uso reportado por cada API)."]
    text = "\n".join(lines) + "\n"
    (OUT / "summary.md").write_text(text, encoding="utf-8")
    (OUT / "summary.json").write_text(json.dumps({k: dict(v) for k, v in arms.items()}, indent=2), encoding="utf-8")
    return text


def run(group: str = "all") -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ("requests.jsonl", "responses.jsonl"):
        path = OUT / name
        if group == "all" or not path.exists():
            path.unlink(missing_ok=True)
        else:  # drop only the earlier OpenAI attempts
            kept = [line for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
                    if json.loads(line)["provider"] != "openai"]
            path.write_text("".join(kept), encoding="utf-8")
    settings = Settings.load(ROOT / "config" / "haiku.toml")
    tax = load_taxonomy(settings)
    rows = dev_rows(SAMPLE)
    steps = [
        ("haiku_cold", lambda: haiku_arm("haiku_cold", rows, tax, settings)),
        ("haiku_warm", lambda: haiku_arm("haiku_warm", dev_rows(WARM_SAMPLE), tax, settings)),
        ("jev_single", lambda: jev_single_arm(rows, tax)),
        *[(f"jev_packed_{n}", lambda n=n: jev_packed_arm(rows[:n], f"jev_packed_{n}")) for n in (10, 25, 50)],
        ("cache_jev", lambda: [jev_single_arm(rows[:1], tax, "cache_jev") for _ in range(3)]),
    ]
    if group == "decisions":
        steps = []
    client = decisions_client()
    steps += [
        ("decisions_single", lambda: decisions_single_arm(client, rows)),
        *[(f"decisions_packed_{n}", lambda n=n: decisions_packed_arm(client, rows[:n], f"decisions_packed_{n}"))
          for n in (10, 50)],
        ("cache_decisions", lambda: [decisions_single_arm(client, rows[:1], "cache_decisions") for _ in range(3)]),
    ]
    for name, step in steps:
        started = time.time()
        step()
        print(f"{name}: {time.time() - started:.1f} s", flush=True)
    print(summarize())


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "summarize"
    if command == "run":
        run(sys.argv[2] if len(sys.argv) > 2 else "all")
    else:
        print(summarize())
