"""Load API keys from the project's .env into the environment. Values are never printed or logged."""
from __future__ import annotations

import os

from .config import ROOT


def load_env(path=ROOT / ".env") -> None:
    """KEY=value lines; blank lines and # comments are skipped. Variables already set are kept."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value and not os.environ.get(key.strip()):
            os.environ[key.strip()] = value


def require(name: str) -> None:
    load_env()
    if not os.environ.get(name):
        raise SystemExit(f"{name} is not set. Put it in .env (see .env.example); it is never committed.")
