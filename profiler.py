"""
Implicit user profiling module.

This module builds and continuously updates a structured "implicit user
profile" purely from the way a person writes during a conversation - no
sign-up form, no questionnaire, no explicit persona sentence typed in by
the user.

The design follows two ideas from the literature reviewed in the thesis:

1. Historical dialogue as the only signal (Ma, Dou, Zhu, Zhong & Wen, 2021,
   "One Chatbot Per Person," SIGIR): a profile is a summary of what the user
   has said and how they said it, refreshed after every turn.
2. Confidence-weighted score aggregation (David, Meidan, Hersko,
   Varnovitzky, Mimran, Elovici & Shabtai, 2025, "ProfiLLM"): a new
   observation is blended into the running profile with a weight that
   decays as more turns arrive, so the profile stabilises instead of
   oscillating on every single message.

Everything here runs with ordinary NLP - no GPU, no external API keys are
required for the profiler itself (only the response generator optionally
calls out to an LLM). That keeps the module runnable in a plain Python
environment, which matters for reproducing the experiments in the thesis.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger("persona_chatbot.profiler")

try:
    from nltk.sentiment.vader import SentimentIntensityAnalyzer
    # Constructing the analyzer is what actually fails if the vader_lexicon
    # corpus was never downloaded (import succeeds either way), so probe it
    # here rather than only at first real use - a silent fallback to the
    # tiny keyword lexicon below would otherwise be very easy to miss.
    SentimentIntensityAnalyzer()
    _VADER_AVAILABLE = True
except Exception:  # pragma: no cover - environment without nltk data
    _VADER_AVAILABLE = False
    logger.warning(
        "NLTK VADER lexicon is not available (import failed or "
        "nltk.download('vader_lexicon') was never run). Falling back to a "
        "small built-in keyword lexicon for sentiment scoring, which is "
        "noticeably less accurate. Run: python -c \"import nltk; "
        "nltk.download('vader_lexicon')\""
    )


# A small topical lexicon used for interest extraction. Keeping this as a
# plain dictionary (rather than a trained classifier) is a deliberate
# choice for the prototype: it is transparent, easy to extend per domain,
# and does not require a labelled training set. Chapter 4 discusses this
# trade-off and how a learned topic model could replace it.
_TOPIC_LEXICON: Dict[str, List[str]] = {
    "sports": ["football", "match", "gym", "workout", "run", "running", "basketball", "training", "fitness"],
    "technology": ["python", "code", "programming", "software", "ai", "computer", "app", "algorithm", "data"],
    "travel": ["trip", "flight", "travel", "vacation", "hotel", "visa", "airport", "tourism"],
    "food": ["restaurant", "recipe", "cooking", "food", "dinner", "coffee", "meal"],
    "education": ["study", "exam", "university", "thesis", "course", "lecture", "school", "degree"],
    "entertainment": ["movie", "series", "music", "game", "playing", "netflix", "song"],
    "health": ["sleep", "stress", "doctor", "tired", "anxious", "health", "sick"],
    "finance": ["money", "budget", "salary", "invest", "price", "expensive", "cheap"],
}

_FORMAL_MARKERS = ["would you", "could you", "please", "kindly", "regards"]
_INFORMAL_MARKERS = ["lol", "haha", "gonna", "wanna", "yeah", "u ", "!!"]

_DECAY_BETA = 0.35  # confidence-decay rate, mirrors ProfiLLM's alpha schedule


def _confidence_weight(turn_index: int, beta: float = _DECAY_BETA) -> float:
    """Weight given to a *new* observation at a given turn.

    Early turns move the profile a lot (the bot knows almost nothing about
    the user yet); later turns move it a little (the running estimate is
    already fairly reliable). This mirrors the decayed weighting scheme
    used for proficiency estimation in ProfiLLM, adapted here to interests,
    tone and sentiment instead of domain expertise.
    """
    return max(0.15, math.exp(-beta * turn_index))


@dataclass
class ImplicitProfile:
    user_id: str
    turn_count: int = 0
    interests: Dict[str, float] = field(default_factory=dict)
    sentiment_ema: float = 0.0          # -1 (negative) .. +1 (positive)
    formality_ema: float = 0.0          # -1 (very informal) .. +1 (very formal)
    avg_message_length: float = 0.0
    emoji_or_punct_energy: float = 0.0  # exclamation/emoji density, proxy for enthusiasm
    dominant_topics: List[str] = field(default_factory=list)
    tone_label: str = "neutral"

    def to_dict(self) -> dict:
        return asdict(self)

    def as_prompt_fragment(self) -> str:
        """Render the profile as a short natural-language brief that a
        response generator can be conditioned on."""
        topics = ", ".join(self.dominant_topics) if self.dominant_topics else "not yet established"
        mood = (
            "generally positive" if self.sentiment_ema > 0.2 else
            "generally negative" if self.sentiment_ema < -0.2 else
            "neutral"
        )
        style = (
            "formal and concise" if self.formality_ema > 0.2 else
            "casual and relaxed" if self.formality_ema < -0.2 else
            "plain, everyday"
        )
        return (
            f"Inferred profile after {self.turn_count} turn(s) - "
            f"likely interests: {topics}; typical mood: {mood}; "
            f"writing style: {style}; tone: {self.tone_label}."
        )


class ProfileStore:
    """Storage backend interface. `FileProfileStore` (below) is the default,
    local-disk implementation used by `python app.py` / gunicorn deployments.
    `KVProfileStore` (cloudflare/kv_store.py) implements the same interface
    on top of Cloudflare KV for the Workers deployment, where there is no
    persistent local filesystem. `ImplicitProfiler` only ever calls these
    two methods, so any other backend (Redis, a real database, ...) is a
    drop-in as long as it implements them the same way.
    """

    def load_raw(self, user_id: str) -> dict | None:
        raise NotImplementedError

    def save_raw(self, user_id: str, data: dict) -> None:
        raise NotImplementedError

    def delete(self, user_id: str) -> None:
        raise NotImplementedError


class FileProfileStore(ProfileStore):
    """Default backend: one JSON file per user under `storage_dir`. Requires
    a writable, persistent local filesystem - fine for `python app.py` or a
    single-process gunicorn deployment, not usable on Cloudflare Workers."""

    def __init__(self, storage_dir: str | Path = "profiles"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, user_id: str) -> Path:
        return self.storage_dir / f"{user_id}.json"

    def load_raw(self, user_id: str) -> dict | None:
        path = self._path(user_id)
        if path.exists():
            return json.loads(path.read_text())
        return None

    def save_raw(self, user_id: str, data: dict) -> None:
        self._path(user_id).write_text(json.dumps(data, indent=2))

    def delete(self, user_id: str) -> None:
        path = self._path(user_id)
        if path.exists():
            path.unlink()


class ImplicitProfiler:
    """Stateful profiler: call `update()` once per user message."""

    def __init__(self, storage_dir: str | Path = "profiles", store: ProfileStore | None = None):
        # `store` takes priority; `storage_dir` is kept for backward
        # compatibility with existing call sites (`ImplicitProfiler(storage_dir=...)`).
        self.store = store if store is not None else FileProfileStore(storage_dir)
        if _VADER_AVAILABLE:
            self._sentiment = SentimentIntensityAnalyzer()
        else:
            self._sentiment = None

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    def _path(self, user_id: str):
        """Kept for backward compatibility with code (and tests) written
        against the old file-only API; only meaningful for FileProfileStore."""
        if isinstance(self.store, FileProfileStore):
            return self.store._path(user_id)
        raise AttributeError("_path() is only available with FileProfileStore")

    def load(self, user_id: str) -> ImplicitProfile:
        data = self.store.load_raw(user_id)
        if data:
            return ImplicitProfile(**data)
        return ImplicitProfile(user_id=user_id)

    def save(self, profile: ImplicitProfile) -> None:
        self.store.save_raw(profile.user_id, profile.to_dict())

    def delete(self, user_id: str) -> None:
        self.store.delete(user_id)

    # ------------------------------------------------------------------ #
    # feature extraction
    # ------------------------------------------------------------------ #
    def _sentiment_score(self, text: str) -> float:
        if self._sentiment is not None:
            return self._sentiment.polarity_scores(text)["compound"]
        # tiny lexicon fallback so the module never hard-fails offline
        pos = {"good", "great", "love", "happy", "awesome", "nice", "thanks", "excellent"}
        neg = {"bad", "hate", "sad", "angry", "terrible", "worst", "annoyed", "tired"}
        tokens = re.findall(r"[a-z']+", text.lower())
        if not tokens:
            return 0.0
        score = sum(1 for t in tokens if t in pos) - sum(1 for t in tokens if t in neg)
        return max(-1.0, min(1.0, score / max(3, len(tokens) // 3)))

    def _formality_score(self, text: str) -> float:
        low = text.lower()
        formal_hits = sum(low.count(m) for m in _FORMAL_MARKERS)
        informal_hits = sum(low.count(m) for m in _INFORMAL_MARKERS)
        if formal_hits == informal_hits == 0:
            return 0.0
        return (formal_hits - informal_hits) / max(1, formal_hits + informal_hits)

    def _topics(self, text: str) -> Counter:
        low = text.lower()
        hits: Counter = Counter()
        for topic, keywords in _TOPIC_LEXICON.items():
            for kw in keywords:
                if kw in low:
                    hits[topic] += 1
        return hits

    def _energy(self, text: str) -> float:
        exclam = text.count("!")
        caps_words = sum(1 for w in text.split() if len(w) > 2 and w.isupper())
        return min(1.0, (exclam + caps_words) / 5.0)

    def _tone_label(self, profile: ImplicitProfile) -> str:
        if profile.sentiment_ema > 0.3 and profile.emoji_or_punct_energy > 0.3:
            return "enthusiastic"
        if profile.sentiment_ema < -0.3:
            return "frustrated"
        if profile.formality_ema > 0.3:
            return "professional"
        if profile.formality_ema < -0.3:
            return "casual"
        return "neutral"

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def update(self, user_id: str, message: str) -> ImplicitProfile:
        profile = self.load(user_id)
        w = _confidence_weight(profile.turn_count)

        sentiment = self._sentiment_score(message)
        formality = self._formality_score(message)
        energy = self._energy(message)
        topic_hits = self._topics(message)

        profile.sentiment_ema = (1 - w) * profile.sentiment_ema + w * sentiment
        profile.formality_ema = (1 - w) * profile.formality_ema + w * formality
        profile.emoji_or_punct_energy = (1 - w) * profile.emoji_or_punct_energy + w * energy
        profile.avg_message_length = (
            (profile.avg_message_length * profile.turn_count + len(message.split()))
            / (profile.turn_count + 1)
        )

        for topic, count in topic_hits.items():
            profile.interests[topic] = profile.interests.get(topic, 0.0) + count
        profile.dominant_topics = [
            t for t, _ in sorted(profile.interests.items(), key=lambda kv: kv[1], reverse=True)[:3]
        ]

        profile.turn_count += 1
        profile.tone_label = self._tone_label(profile)

        self.save(profile)
        return profile
