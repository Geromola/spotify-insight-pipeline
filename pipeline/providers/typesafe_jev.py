"""Jev (TypeSafe System One): fixed-choice classification of one review per call.

Jev returns probabilities instead of text, so it maps straight onto the shared labels with
no parsing. It cannot write text: the evidence quote is a Choice over code-made segments and
entities come from code, so every field still lands in the contract schema.
"""
from __future__ import annotations

import time
import uuid

from typesafe_sdk import Choice, RetryPolicy, Score, TypeSafeClient
from typesafe_sdk import TypeSafeAPIConnectionError, TypeSafeAPIError, TypeSafeRateLimitError

from ..config import CONTEXT_MEANINGS
from ..env import require
from ..textproc import entities

QUESTIONS_VERSION = "jev-q2"  # q2 adds the free-tier limit and plan questions
DIAGNOSIS_VERSION = "jev-d1"  # the blind check of the diagnose stage: symptom and context

TOPICS = {
    "access": "Login, signup, password or account access",
    "usability": "Navigation, controls, layout, queue or playlist management, ad interruptions",
    "playback": "Playback failure, crashes, lag, connection failures, audio quality, battery or data use",
    "downloads": "Downloading, saved music, offline listening, disappearing downloads",
    "catalog": "Missing songs or artists, search and discovery, recommendations, lyrics availability",
    "billing": "Price, charges, subscriptions, paywalls, premium entitlement, explicitly premium-only controls",
    "support": "Contacting customer support and the support response",
    "other": "General praise or criticism, unrelated content, or no specific supported topic",
}
INTENTS = {
    "cancellation": "Explicitly leaving, uninstalling, cancelling or switching away, or threatening to; wins over complaint",
    "complaint": "A negative experience, including mixed praise and criticism",
    "request": "Asks for a change without reporting any failure",
    "praise": "Only positive about the app",
    "unclear": "Meaningless or unrelated text, or a bare boycott slogan with no product complaint",
}
SEVERITY = {
    "1": "No problem reported: praise, neutral or unclear content, or a pure feature request",
    "2": "Dislike, generic criticism, minor annoyance or cosmetic issue, no lost function stated",
    "3": "A function is degraded or restricted but some use or a workaround remains",
    "4": "A core task is clearly blocked, such as being unable to log in or play music",
    "5": "Explicit serious financial, privacy or data harm; an expensive plan, a crash or anger alone is not 5",
}
SENTIMENT = ["very negative", "negative", "neutral", "positive", "very positive"]
LIMIT = {
    "yes": "Complains about something the free version restricts by design: ads between songs, shuffle-only play, a skip "
           "limit, not being able to pick a song or play in order, seeking, or a feature locked to premium",
    "no": "Anything else: bugs, crashes, redesigns, catalog gaps, login problems, price alone, praise, unclear text",
}
PLAN = {
    "paid": "The reviewer says they pay or have premium, family, duo or student",
    "free": "The reviewer says they use the free version or clearly that they don't pay",
    "unstated": "The reviewer does not say; a complaint about premium-only features alone does not reveal the plan",
}


class JevClassifier:
    name = "typesafe"
    tier = "standard"
    questions_version = QUESTIONS_VERSION
    diagnosis_version = DIAGNOSIS_VERSION

    def __init__(self, model: str = "jev-1.13.0", confidence_floor: float = 0.5, timeout: float = 30.0):
        require("TYPESAFE_API_KEY")
        # SDK retries are off: the caller retries and logs every attempt.
        self.client = TypeSafeClient(model=model, retry=RetryPolicy(max_retries=0), timeout=timeout)
        self.model, self.confidence_floor = model, confidence_floor

    def questions(self, segs: list[str], tax: dict) -> dict:
        questions = {
            "topic": Choice(instructions="Which product area is this Spotify app review mainly about? With several "
                                         "problems, the most severe one; for praise, the first praised feature.",
                            criteria=TOPICS),
            "intent": Choice(instructions="What is the reviewer doing?", criteria=INTENTS),
            "severity": Choice(instructions="How severe is the impact the review actually reports? Judge the stated "
                                            "impact, not the anger or the star rating.", criteria=SEVERITY),
            "sentiment": Score(instructions="Overall tone of the review.", criteria=SENTIMENT),
            "limit": Choice(instructions="Is the review about a limitation the free version has by design?", criteria=LIMIT),
            "plan": Choice(instructions="What does the reviewer say about their own Spotify plan?", criteria=PLAN),
        }
        if len(segs) > 1:
            questions["quote"] = Choice(instructions="Which part of the review best supports its main problem or praise?",
                                        criteria={str(i): seg for i, seg in enumerate(segs)})
        for topic, subs in tax["topics"].items():  # fan out: one question per topic that has subtopics
            if len(subs) > 1:
                questions[f"sub_{topic}"] = Choice(
                    instructions=f"If the review is about {topic}, which kind of {topic} issue is it?",
                    criteria={sub["id"]: sub["definition"] for sub in subs})
        return questions

    def classify(self, text: str, segs: list[str], tax: dict) -> dict:
        """One review, one call. Returns the contract record plus probabilities, confidence and usage."""
        started = time.time()
        try:
            response = self.client.system_one(state=text, questions=self.questions(segs, tax))
        except TypeSafeRateLimitError as e:
            return self._failed("rate_limited", e, started, retry_after=(e.retry_after_ms or 0) / 1000 or None)
        except TypeSafeAPIError as e:
            return self._failed(f"http_{e.status}", e, started, fatal=e.status in (400, 401, 403, 404, 422))
        except TypeSafeAPIConnectionError as e:
            return self._failed(f"connection: {type(e).__name__}", e, started)
        choices, scores = response.choices, response.scores
        topic = choices["topic"].choice
        subs = tax["topics"][topic]
        subtopic = choices[f"sub_{topic}"].choice if f"sub_{topic}" in choices else subs[0]["id"]
        quote = segs[int(choices["quote"].choice)] if "quote" in choices else segs[0]
        confidence = {name: choices[name].confidence for name in ("topic", "intent", "severity")}
        record = {"topic": topic, "subtopic": subtopic, "intent": choices["intent"].choice,
                  "sentiment": round(scores["sentiment"].score / 2 - 1, 3),  # 0..4 -> -1..1
                  "severity": int(choices["severity"].choice), "entities": entities(text),
                  "evidence_quote": quote, "needs_review": min(confidence.values()) < self.confidence_floor,
                  "free_tier_limit": choices["limit"].choice == "yes", "plan": choices["plan"].choice}
        return {"ok": True, "record": record, "confidence": confidence,
                "probabilities": {name: dict(choices[name].probabilities) for name in ("topic", "intent", "severity")},
                "request_id": response.request_id or f"jev-{uuid.uuid4().hex}", "model": response.model,
                "input_tokens": response.usage.input_tokens or 0, "output_tokens": response.usage.output_tokens or 0,
                "usage_available": response.usage.input_tokens is not None, "started_at": started,
                "duration_ms": int((time.time() - started) * 1000)}

    @staticmethod
    def diagnosis_questions(symptoms: list[dict]) -> dict:
        """The blind check of one diagnosis: the area's symptoms (+ other) and the device or context."""
        criteria = {s["id"]: f"{s['name']}: {s['definition']}" for s in symptoms}
        criteria["other"] = "None of these, or the review names no concrete symptom"
        return {"symptom": Choice(instructions="Which concrete problem does this Spotify Android app complaint describe? "
                                               "With several, the most severe one.", criteria=criteria),
                "context": Choice(instructions="Which device or context does the reviewer say the problem happens with?",
                                  criteria=CONTEXT_MEANINGS)}

    def check_diagnosis(self, text: str, symptoms: list[dict]) -> dict:
        """One review, one call. Returns the symptom id and context word with confidence and usage."""
        started = time.time()
        try:
            response = self.client.system_one(state=text, questions=self.diagnosis_questions(symptoms))
        except TypeSafeRateLimitError as e:
            return self._failed("rate_limited", e, started, retry_after=(e.retry_after_ms or 0) / 1000 or None)
        except TypeSafeAPIError as e:
            return self._failed(f"http_{e.status}", e, started, fatal=e.status in (400, 401, 403, 404, 422))
        except TypeSafeAPIConnectionError as e:
            return self._failed(f"connection: {type(e).__name__}", e, started)
        choices = response.choices
        return {"ok": True, "record": {"symptom_id": choices["symptom"].choice, "context": choices["context"].choice},
                "confidence": {name: choices[name].confidence for name in ("symptom", "context")},
                "probabilities": {name: dict(choices[name].probabilities) for name in ("symptom", "context")},
                "request_id": response.request_id or f"jev-{uuid.uuid4().hex}", "model": response.model,
                "input_tokens": response.usage.input_tokens or 0, "output_tokens": response.usage.output_tokens or 0,
                "usage_available": response.usage.input_tokens is not None, "started_at": started,
                "duration_ms": int((time.time() - started) * 1000)}

    @staticmethod
    def _failed(error: str, exc, started: float, retry_after=None, fatal=False) -> dict:
        return {"ok": False, "error": error, "request_id": getattr(exc, "request_id", None) or f"jev-err-{uuid.uuid4().hex[:8]}",
                "retry_after": retry_after, "fatal": fatal, "input_tokens": 0, "output_tokens": 0,
                "usage_available": False, "started_at": started, "duration_ms": int((time.time() - started) * 1000)}
