"""Provider adapters. Every text-generating role talks to a model through the same small interface."""
from __future__ import annotations

from pathlib import Path

from .base import ModelRequest, ModelResponse, Provider

__all__ = ["ModelRequest", "ModelResponse", "Provider", "get_provider"]


def get_provider(name: str, model: str, run_dir: Path, **options) -> Provider:
    if name == "fake":
        from .fake import FakeProvider
        return FakeProvider(model=model, spool=Path(run_dir) / "fake_batches", **options)
    if name == "anthropic":
        from .anthropic_haiku import AnthropicProvider
        return AnthropicProvider(model=model)
    raise NotImplementedError(f"Unknown provider '{name}'. Jev is a classifier, used through JevClassifier.")
