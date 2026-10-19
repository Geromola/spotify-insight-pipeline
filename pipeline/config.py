"""Run settings, the shared label codes and the label_config identity."""
from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
RUNS = ROOT / "runs"
SCHEMA_VERSION = "s2"  # s2 adds the free-tier limit and plan fields

# Short codes keep model output small; code maps them back to the contract's exact labels.
TOPIC_CODES = {"ac": "access", "us": "usability", "pb": "playback", "dl": "downloads",
               "ca": "catalog", "bi": "billing", "su": "support", "ot": "other"}
INTENT_CODES = {"x": "cancellation", "c": "complaint", "r": "request", "p": "praise", "u": "unclear"}
COMPLAINT_INTENTS = ("complaint", "cancellation")

# Diagnosis: the question's four areas (billing and support share one), each with a short input code.
DIAGNOSIS_AREAS = {"access": ("access",), "usability": ("usability",), "playback": ("playback",),
                   "billing/support": ("billing", "support")}
AREA_CODES = {"ac": "access", "us": "usability", "pb": "playback", "bs": "billing/support"}
# Disjoint alphabets on purpose: a code written into the wrong field is caught instead of read as valid.
CONTEXT_CODES = {"B": "bluetooth", "C": "car", "T": "tv-cast", "W": "wearable", "D": "desktop-web", "N": "none"}
TRIGGER_CODES = {"u": "after-update", "s": "background", "o": "offline", "k": "app-start", "x": "none"}
LONG_TIME_CODES = {"y": True, "-": False}
CONTEXT_MEANINGS = {
    "bluetooth": "Bluetooth, headphones, earbuds or a speaker",
    "car": "A car or Android Auto",
    "tv-cast": "A TV, casting, Spotify Connect or a smart speaker",
    "wearable": "A watch or another wearable",
    "desktop-web": "The desktop app or the web player",
    "none": "No device or context stated (the phone itself is implied)",
}


@dataclass(frozen=True)
class Settings:
    provider: str = "fake"
    model: str = "fake-rules-v0"
    effort: str = "low"
    thinking: str = "disabled"          # primary pass: no thinking tokens at all
    fallback_effort: str = "low"
    fallback_thinking: str = "adaptive" # the fallback step is where the model may think
    mode: str = "sync"                  # sync: one request at a time; batch: provider Batch API in waves
    workers: int = 1
    batch_size: int = 50                # reviews per enrichment request; the contract's ceiling is 50
    max_request_chars: int = 16000      # second bound on a request, a proxy for its token budget
    quote_full_max: int = 80            # texts up to this length quote themselves; longer ones are segmented
    max_output_tokens: int = 1400       # hard cap per enrichment request
    output_tokens_per_item: int = 26    # the s2 line measured 21.6 tokens per review at worst (format probe)
    fallback_max_output_tokens: int = 2000   # thinking counts toward this cap
    retry_batch_size: int = 10
    fallback_fraction: float = 0.02     # share of unique texts that may go to the fallback step
    batch_file_requests: int = 2000     # requests per provider batch file
    max_enqueued_tokens: int = 18_000_000
    poll_seconds: float = 30.0
    transport_retries: int = 4
    backoff_seconds: float = 2.0
    budget_usd: float = 15.0
    enrich_cap_usd: float = 8.0
    fallback_cap_usd: float = 1.5
    taxonomy: str = "taxonomy.json"
    verifier: str = "fake"              # fake | jev
    verify_fraction: float = 0.2        # declared sample: this share of unique texts...
    verify_max: int = 1000              # ...capped at this many
    verify_seed: str = "a5-verify-v1"
    roles_cap_usd: float = 0.5          # verify + group + memo + critic together
    writer_thinking: str = "adaptive"   # group, memo and critic may think; they make a handful of calls
    writer_effort: str = "medium"
    memo_effort: str = "medium"         # "high" spent the whole 16,000-token budget thinking (2026-10-18)
    writer_max_tokens: int = 16000      # thinking counts toward it; discovery needed more than 8,000
    group_examples_per_issue: int = 6
    discover_examples_per_topic: int = 40
    memo_top_issues: int = 5
    memo_quotes_per_issue: int = 4
    symptoms: str = "symptoms.json"     # the approved symptom catalog, under prompts/ unless absolute
    symptom_examples: int = 30          # discovery pack per area: this many most-liked plus as many spread out
    diagnose_cap_usd: float = 1.5       # the diagnose stage and its independent check
    diagnose_tokens_per_item: int = 14  # output cap per diagnosis line (n|s|c|t|y|q), plus 40 per request
    diagnose_thinking: str = "adaptive"  # evals/diagnose_probe: 75% symptom agreement with Jev, against 56% without
    diagnose_effort: str = "low"
    diagnose_max_output_tokens: int = 10000  # with thinking: the probe's largest 50-review request used 7,636
    diagnose_verify_n: int = 300        # declared sample of completed diagnoses for the blind check
    diagnose_verify_seed: str = "diagnose-verify-v1"

    @classmethod
    def load(cls, path: Path | None = None, **overrides) -> "Settings":
        values = tomllib.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        known = {f.name for f in fields(cls)}
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"Unknown settings: {sorted(unknown)}")
        settings = cls(**values)
        settings = replace(settings, **{k: v for k, v in overrides.items() if v is not None})
        if not 1 <= settings.batch_size <= 50:
            raise ValueError("batch_size must be between 1 and 50")
        return settings

    def as_dict(self) -> dict:
        return asdict(self)


def load_taxonomy(settings: Settings) -> dict:
    tax = json.loads((PROMPTS / settings.taxonomy).read_text(encoding="utf-8"))
    missing = set(TOPIC_CODES.values()) - set(tax["topics"])
    if missing:
        raise ValueError(f"Taxonomy lacks topics: {sorted(missing)}")
    return tax


def label_config(settings: Settings, tax: dict) -> str:
    """One identity for the whole enrichment policy (primary + fallback + schema).

    The grader flags any successful enrich call whose label_config differs from the final
    record's, so the primary and fallback steps must share this single string.
    """
    from .textproc import ENTITY_VERSION  # local import: textproc has no dependency on config

    digest = hashlib.sha256()
    for name in ("labels.md", "enrich_v2.md", "fallback_v2.md"):
        digest.update((PROMPTS / name).read_bytes())
    digest.update(json.dumps(tax, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    digest.update(f"{settings.quote_full_max}|{ENTITY_VERSION}".encode())
    return (f"{settings.provider}:{settings.model}|{settings.thinking}/{settings.effort}"
            f">{settings.fallback_thinking}/{settings.fallback_effort}"
            f"|prompt={digest.hexdigest()[:10]}|tax={tax['version']}|schema={SCHEMA_VERSION}")
