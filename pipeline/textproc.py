"""Deterministic text work that needs no model: quote segments, entities, prompt rendering."""
from __future__ import annotations

import math
import re

ENTITY_VERSION = "e1"
MIN_SEGMENT_CHARS = 25
MAX_SEGMENTS = 10

# Chunks tile the text exactly: a run ending in sentence punctuation or a line break, or the tail.
_CHUNK = re.compile(r"[^.!?\n]*[.!?\n]+|[^.!?\n]+$")
_SPACE = re.compile(r"\s+")


def segments(text: str, full_max: int) -> list[str]:
    """Split a review into candidate evidence quotes.

    Every segment is an exact substring of `text`, so a quote chosen by index always passes
    the grader's substring check. Short reviews quote themselves.
    """
    whole = text.strip()
    if len(text) <= full_max:
        return [whole]
    spans: list[tuple[int, int]] = []
    start = None
    for match in _CHUNK.finditer(text):
        if start is None:
            start = match.start()
        if len(text[start:match.end()].strip()) >= MIN_SEGMENT_CHARS:
            spans.append((start, match.end()))
            start = None
    if start is not None:  # a short tail joins the previous segment
        if spans:
            spans[-1] = (spans[-1][0], len(text))
        else:
            spans.append((start, len(text)))
    if len(spans) > MAX_SEGMENTS:
        spans[MAX_SEGMENTS - 1:] = [(spans[MAX_SEGMENTS - 1][0], spans[-1][1])]
    parts = [text[a:b].strip() for a, b in spans]
    return [p for p in parts if p] or [whole]


def render(idx: int, segs: list[str]) -> str:
    """One prompt line per review. Whitespace is collapsed here only; quotes come from `segs`."""
    if len(segs) == 1:
        return f"{idx}: {_SPACE.sub(' ', segs[0])}"
    return f"{idx}: " + " ".join(f"[{j}] {_SPACE.sub(' ', s)}" for j, s in enumerate(segs))


def estimate_tokens(chars: int) -> int:
    """Conservative size estimate used only for spend reservations and queue limits."""
    return math.ceil(chars / 3.0)


# Product terms matched literally in the review, so every entity is supported by the text.
_ENTITY_PATTERNS = {
    "ads": r"ads?|adverts?|advertisements?|commercials?",
    "shuffle": r"shuffle[sd]?|shuffling",
    "skips": r"skips?|skipping|skipped",
    "premium": r"premium",
    "free tier": r"free (?:version|tier|plan|users?)",
    "price": r"price[sd]?|pricing|expensive|costly",
    "subscription": r"subscriptions?|subscribe[ds]?",
    "payment": r"payments?|charged?|charges|billing|billed|refunds?",
    "family plan": r"family (?:plan|sub\w*)|duo",
    "student plan": r"student (?:plan|discount|sub\w*)",
    "playlist": r"playlists?",
    "queue": r"queue",
    "liked songs": r"liked songs",
    "library": r"library",
    "podcasts": r"podcasts?",
    "audiobooks": r"audiobooks?",
    "lyrics": r"lyrics?",
    "search": r"search(?:ing|es|ed)?",
    "recommendations": r"recommend(?:ation|ed|s)?s?|suggest(?:ion|ed)s?|discover weekly",
    "dj": r"dj",
    "wrapped": r"wrapped",
    "offline": r"offline",
    "downloads": r"downloads?|downloaded|downloading",
    "login": r"log ?in|logging in|sign ?in|logged out|log ?out",
    "password": r"password",
    "account": r"accounts?",
    "update": r"updates?|updated|updating",
    "crash": r"crash(?:es|ed|ing)?|freez(?:e|es|ing)|froze",
    "buffering": r"buffer(?:ing|s)?|lag(?:s|gy|ging)?|loading",
    "audio quality": r"(?:sound|audio|music) quality",
    "bluetooth": r"bluetooth",
    "car": r"android auto|carplay|car mode|car",
    "cast": r"chromecast|cast(?:ing)?|spotify connect",
    "widget": r"widget",
    "notifications": r"notifications?",
    "home screen": r"home ?screen|home ?page",
    "interface": r"ui|interface|layout|design",
    "video": r"videos?|canvas",
    "sleep timer": r"sleep timer",
    "equalizer": r"equali[sz]er",
    "battery": r"battery",
    "storage": r"storage|memory",
    "support": r"customer (?:service|support|care)|support team|help ?desk",
}
_ENTITIES = [(name, re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE)) for name, pattern in _ENTITY_PATTERNS.items()]


def entities(text: str) -> list[str]:
    return [name for name, pattern in _ENTITIES if pattern.search(text)]
