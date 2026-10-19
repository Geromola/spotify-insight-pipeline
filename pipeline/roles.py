"""Shared plumbing for the verify, group and memo roles: call log, budget, JSON calls, input-keyed cache."""
from __future__ import annotations

import hashlib
import json
import random
import time
import uuid

from .budget import BudgetStop, Rates, admit
from .config import PROMPTS, Settings
from .db import tx
from .providers import ModelRequest, ModelResponse, Provider
from .textproc import estimate_tokens


def prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8").strip()


def digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def record_call(conn, *, role: str, step: str, provider: str, model: str, effort: str | None, tier: str,
                label_config: str, invocation: int, resp: ModelResponse, review_ids: list[str], cost: float,
                outcome: str | None = None, n_valid: int | None = None, n_invalid: int | None = None) -> None:
    """One row per attempted call, the same table the enricher writes, so calls.jsonl covers every role."""
    with tx(conn):
        conn.execute(
            "INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (resp.request_id, None, role, step, provider, model, effort, tier, label_config, invocation,
             outcome or ("succeeded" if resp.ok else "failed"), resp.error, resp.input_tokens,
             resp.cached_input_tokens, resp.cache_write_tokens, resp.output_tokens, resp.reasoning_tokens,
             int(resp.usage_available), cost, resp.started_at, resp.duration_ms, resp.stop_reason, n_valid,
             n_invalid, json.dumps(review_ids), resp.text))


def cached(conn, kind: str, key: str) -> dict | None:
    row = conn.execute("SELECT output FROM artifacts WHERE kind=? AND input_hash=?", (kind, key)).fetchone()
    return json.loads(row["output"]) if row else None


def remember(conn, kind: str, key: str, output: dict) -> None:
    with tx(conn):
        conn.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?)",
                     (kind, key, json.dumps(output, ensure_ascii=False), time.time()))


class Writer:
    """A model role that reads a bounded JSON pack and must answer with one JSON object."""

    def __init__(self, conn, provider: Provider, settings: Settings, rates: Rates, invocation: int,
                 sleep=time.sleep):
        self.conn, self.provider, self.s, self.rates, self.invocation = conn, provider, settings, rates, invocation
        self.sleep = sleep

    def label_config(self, prompt_name: str) -> str:
        return (f"{self.provider.name}:{self.provider.model}|{self.s.writer_thinking}/{self.s.writer_effort}"
                f"|prompt={digest(prompt(prompt_name))[:10]}")

    def ask(self, *, role: str, step: str, prompt_name: str, pack: dict, schema: dict,
            review_ids: list[str]) -> dict:
        """Send the pack, validate the JSON, log every attempt. Raises BudgetStop or RuntimeError."""
        system, user = prompt(prompt_name), json.dumps(pack, ensure_ascii=False, indent=1)
        tier = self.provider.sync_tier
        reservation = self.rates.cost(self.provider.name, self.provider.model, tier,
                                      estimate_tokens(len(system) + len(user)), 0, self.s.writer_max_tokens)
        admit(self.conn, self.s, step, reservation)
        failures = 0
        while True:
            request = ModelRequest(f"{role[0]}{self.invocation}-{uuid.uuid4().hex[:14]}", role, step, system, user,
                                   self.s.writer_effort, self.s.writer_max_tokens, self.s.writer_thinking, schema)
            resp = self.provider.complete(request)
            parsed, error = None, resp.error
            if resp.ok:
                try:
                    parsed = json.loads(resp.text)
                    error = None if isinstance(parsed, dict) else "not_a_json_object"
                except json.JSONDecodeError:
                    error = f"invalid_json:{resp.stop_reason}"
            cost = self.rates.cost(self.provider.name, self.provider.model, tier, resp.input_tokens,
                                   resp.cached_input_tokens, resp.output_tokens, resp.cache_write_tokens)
            ok = resp.ok and error is None
            resp.error = error
            record_call(self.conn, role=role, step=step, provider=self.provider.name, model=self.provider.model,
                        effort=self.s.writer_effort, tier=tier, label_config=self.label_config(prompt_name),
                        invocation=self.invocation, resp=resp, review_ids=review_ids, cost=cost,
                        outcome="succeeded" if ok else "failed")
            if ok:
                return parsed
            failures += 1
            if resp.fatal or failures > self.s.transport_retries:
                raise RuntimeError(f"{role}/{step} failed: {error}")
            self.sleep(min(60.0, self.s.backoff_seconds * 2 ** (failures - 1)) * (0.5 + random.random()))


__all__ = ["BudgetStop", "Writer", "cached", "digest", "prompt", "record_call", "remember"]
