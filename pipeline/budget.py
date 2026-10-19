"""Rates and the spend ledger: reserve the worst case before a call, settle with real usage after."""
from __future__ import annotations

import csv
from pathlib import Path

from .config import ROOT, Settings

UNITS = {"per_1M_tokens": 1_000_000, "per_token": 1}
RATES_PATH = ROOT / "cost" / "rates.csv"


class BudgetStop(Exception):
    """Raised when admitting the next request would cross a spending cap."""


class Rates:
    def __init__(self, path: Path = RATES_PATH, multiplier: float = 1.0):
        self.prices: dict[tuple[str, str, str, str], float] = {}
        with Path(path).open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                key = (row["provider"], row["model"], row["tier"], row["item"])
                self.prices[key] = float(row["price_usd"]) / UNITS[row["unit"]] * multiplier

    def price(self, provider: str, model: str, tier: str, item: str) -> float:
        try:
            return self.prices[(provider, model, tier, item)]
        except KeyError:
            raise KeyError(f"No rate for {provider}/{model}/{tier}/{item} in rates.csv; add it rather than assume zero")

    def cost(self, provider: str, model: str, tier: str, input_tokens: int, cached_input_tokens: int,
             output_tokens: int, cache_write_tokens: int = 0) -> float:
        """Mutually exclusive items: uncached input, cache writes, cache reads, output.

        `input_tokens` is all input; thinking is already inside output and is not added again.
        """
        uncached = input_tokens - cached_input_tokens - cache_write_tokens
        return (uncached * self.price(provider, model, tier, "input")
                + cache_write_tokens * self.price(provider, model, tier, "cache_write")
                + cached_input_tokens * self.price(provider, model, tier, "cached_input")
                + output_tokens * self.price(provider, model, tier, "output"))


def spent(conn, step: str | None = None) -> float:
    sql, args = "SELECT COALESCE(SUM(cost_usd), 0) FROM calls", ()
    if step:
        sql, args = sql + " WHERE step=?", (step,)
    return conn.execute(sql, args).fetchone()[0]


def reserved(conn, step: str | None = None) -> float:
    sql, args = "SELECT COALESCE(SUM(reserved_usd), 0) FROM requests WHERE status IN ('built','submitted')", ()
    if step:
        sql, args = sql + " AND step=?", (step,)
    return conn.execute(sql, args).fetchone()[0]


def admit(conn, settings: Settings, step: str, next_reservation: float) -> None:
    """Stop admitting work when spent + reserved + the next reservation would exceed a cap."""
    total = spent(conn) + reserved(conn) + next_reservation
    if total > settings.budget_usd:
        raise BudgetStop(f"total budget: {total:.4f} > {settings.budget_usd:.2f} USD")
    caps = {"primary": settings.enrich_cap_usd, "fallback": settings.fallback_cap_usd,
            "diagnose": settings.diagnose_cap_usd}  # diagnose covers the classifier and its blind check
    if step in caps:
        cap, stage = caps[step], spent(conn, step) + reserved(conn, step) + next_reservation
    else:  # verify, group, memo and critic draw on one shared cap
        cap = settings.roles_cap_usd
        stage = (spent(conn) - sum(spent(conn, s) for s in caps)
                 + reserved(conn) - sum(reserved(conn, s) for s in caps) + next_reservation)
    if stage > cap:
        raise BudgetStop(f"{step} cap: {stage:.4f} > {cap:.2f} USD")
