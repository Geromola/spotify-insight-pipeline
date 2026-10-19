"""Claude through the official Anthropic SDK: the Messages API (sync lane) and Message Batches (batch lane)."""
from __future__ import annotations

import time
import uuid

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from ..env import require
from .base import ModelRequest, ModelResponse, Provider


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str = "claude-haiku-5-5", timeout: float = 120.0):
        require("ANTHROPIC_API_KEY")
        # SDK retries are off: the pipeline retries with its own backoff so every attempt is logged.
        self.client = anthropic.Anthropic(max_retries=0, timeout=timeout)
        self.model = model

    def _params(self, request: ModelRequest) -> dict:
        output_config = {"effort": request.effort}
        if request.schema:
            output_config["format"] = {"type": "json_schema", "schema": request.schema}
        return {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            # The frozen prefix (instructions, definitions, taxonomy) is cached; reviews come after it.
            "system": [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": request.user}],
            "thinking": {"type": request.thinking},
            "output_config": output_config,
        }

    def _response(self, message, started: float, timed: bool = True) -> ModelResponse:
        usage = message.usage
        reads = usage.cache_read_input_tokens or 0
        writes = usage.cache_creation_input_tokens or 0
        details = getattr(message, "stop_details", None)
        return ModelResponse(
            request_id=message.id, ok=True,
            text="".join(block.text for block in message.content if block.type == "text"),
            input_tokens=usage.input_tokens + reads + writes, cached_input_tokens=reads, cache_write_tokens=writes,
            output_tokens=usage.output_tokens, stop_reason=message.stop_reason,
            refusal_category=getattr(details, "category", None) if message.stop_reason == "refusal" else None,
            started_at=started, duration_ms=int((time.time() - started) * 1000) if timed else 0)

    def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.time()

        def failed(error: str, exc=None, **extra) -> ModelResponse:
            request_id = getattr(exc, "request_id", None) or f"{request.key}-err-{uuid.uuid4().hex[:8]}"
            return ModelResponse(request_id=request_id, ok=False, error=error[:300], usage_available=False,
                                 started_at=started, duration_ms=int((time.time() - started) * 1000), **extra)

        try:
            return self._response(self.client.messages.create(**self._params(request)), started)
        except anthropic.AuthenticationError as e:
            return failed("authentication: check ANTHROPIC_API_KEY", e, fatal=True)
        except anthropic.PermissionDeniedError as e:
            return failed(f"permission_denied: {e.message}", e, fatal=True)
        except anthropic.BadRequestError as e:
            return failed(f"bad_request: {e.message}", e, fatal=True)
        except anthropic.NotFoundError as e:
            return failed(f"not_found: {e.message}", e, fatal=True)
        except anthropic.RateLimitError as e:
            retry_after = e.response.headers.get("retry-after")
            return failed("rate_limited", e, retry_after=float(retry_after) if retry_after else None)
        except anthropic.APIStatusError as e:
            return failed(f"http_{e.status_code}: {e.message}", e)
        except anthropic.APIConnectionError as e:  # includes timeouts
            return failed(f"connection: {type(e).__name__}", e)

    def submit_batch(self, requests: list[ModelRequest]) -> str:
        batch = self.client.messages.batches.create(requests=[
            Request(custom_id=r.key, params=MessageCreateParamsNonStreaming(**self._params(r))) for r in requests])
        return batch.id

    def unrecorded_batches(self, since: float, known: set[str]) -> list[str]:
        """Batches created since `since` that the pipeline has not recorded (after a failed create call)."""
        try:
            return [b.id for b in self.client.messages.batches.list(limit=20)
                    if b.id not in known and b.created_at.timestamp() >= since - 60]
        except anthropic.APIError:
            return []

    def poll_batch(self, provider_batch: str) -> str:
        status = self.client.messages.batches.retrieve(provider_batch).processing_status
        return "completed" if status == "ended" else "in_progress"

    def fetch_batch(self, provider_batch: str) -> dict[str, ModelResponse]:
        """Results arrive in any order, so they are keyed by custom_id, never by position."""
        collected, responses = time.time(), {}
        for item in self.client.messages.batches.results(provider_batch):
            result = item.result
            if result.type == "succeeded":
                responses[item.custom_id] = self._response(result.message, collected, timed=False)
            else:  # errored, canceled or expired: not billed, and the texts go back to the backlog
                error = f"batch_{result.type}"
                if result.type == "errored":
                    error += f": {result.error.type}"
                responses[item.custom_id] = ModelResponse(
                    request_id=f"{provider_batch}-{item.custom_id}", ok=False, error=error,
                    usage_available=False, started_at=collected)
        return responses
