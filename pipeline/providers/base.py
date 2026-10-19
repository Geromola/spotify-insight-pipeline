"""The request/response shapes shared by all providers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ModelRequest:
    key: str                # our id; doubles as the Batch API custom_id
    role: str               # enrich | verify | group | memo
    step: str
    system: str             # static prefix first, so provider prompt caching can apply
    user: str
    effort: str
    max_output_tokens: int
    thinking: str = "disabled"   # disabled | adaptive
    schema: dict | None = None   # JSON schema: the response must be one JSON object matching it


@dataclass
class ModelResponse:
    request_id: str         # provider request id, unique per attempt
    ok: bool                # False for transport/API failures and unfinished batch lines
    text: str = ""
    error: str | None = None
    input_tokens: int = 0           # all input: uncached + cache writes + cache reads
    cached_input_tokens: int = 0    # cache reads
    cache_write_tokens: int = 0
    output_tokens: int = 0          # includes any thinking tokens
    reasoning_tokens: int = 0
    usage_available: bool = True
    stop_reason: str | None = None  # end_turn | max_tokens | refusal | ...
    refusal_category: str | None = None
    retry_after: float | None = None
    fatal: bool = False             # an error retrying cannot fix (bad key, invalid request)
    started_at: float = 0.0
    duration_ms: int = 0


class Provider:
    name: str
    model: str
    sync_tier: str = "standard"
    batch_tier: str = "batch"

    def complete(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    def submit_batch(self, requests: list[ModelRequest]) -> str:
        raise NotImplementedError

    def poll_batch(self, provider_batch: str) -> str:
        """Returns in_progress or completed."""
        raise NotImplementedError

    def fetch_batch(self, provider_batch: str) -> dict[str, ModelResponse]:
        """Responses by request key. A key that is absent did not finish."""
        raise NotImplementedError
