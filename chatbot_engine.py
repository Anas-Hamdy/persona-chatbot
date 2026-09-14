"""
Persona-conditioned response generation.

Two back-ends are supported behind one interface, `ChatEngine.reply()`:

* **LLM back-end** - if an API key is present in the environment
  (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`), the engine builds a system
  prompt that injects the user's implicit profile (see profiler.py) and
  calls the corresponding chat completion endpoint. This is the
  "conditional generation" strategy described in Chapter 3: instead of
  training a persona-conditioned decoder from scratch (which needs a GPU
  cluster and a much larger corpus than is available for a master's
  thesis), the persona is injected as conditioning context at inference
  time - a well-documented, cheaper approximation of the same idea that
  keeps the underlying language model's fluency intact.

* **Offline fallback back-end** - if no key is configured, a lightweight
  retrieval + template generator produces a reasonable, profile-aware
  reply so the whole system is runnable and demonstrable without any paid
  API access. This keeps the thesis prototype self-contained.
"""

from __future__ import annotations

import logging
import os
from typing import List, Dict

from profiler import ImplicitProfile

logger = logging.getLogger("persona_chatbot.engine")


SYSTEM_PROMPT_TEMPLATE = """You are a personalized conversational assistant.
Adapt your tone, vocabulary, and the examples you use to match the user
profile below, which was inferred implicitly from the conversation itself
(the user never filled in any form). Do not mention that you are using a
profile - just let the reply feel naturally suited to this person.

{profile_brief}

Keep replies concise (2-4 sentences) and natural."""


class _OfflineGenerator:
    """A small, transparent template engine used when no LLM key is set.

    It is intentionally simple: the point of the prototype is to show that
    the *profile* changes the shape of the answer, not to compete with a
    large pretrained model on open-domain fluency.
    """

    _OPENERS = {
        "enthusiastic": ["That's great to hear!", "Love the energy!"],
        "frustrated": ["I hear you, that sounds rough.", "Sorry that's been frustrating."],
        "professional": ["Understood.", "Noted, thank you for the detail."],
        "casual": ["Cool,", "Gotcha,"],
        "neutral": ["Got it.", "Okay,"],
    }

    _TOPIC_FOLLOWUPS = {
        "sports": "Are you training for anything specific right now?",
        "technology": "Are you working with a particular language or framework?",
        "travel": "Where are you thinking of going next?",
        "food": "Do you cook that yourself or go out for it?",
        "education": "How is the workload treating you at the moment?",
        "entertainment": "Anything you'd especially recommend?",
        "health": "Have you been able to get enough rest lately?",
        "finance": "Are you planning around that or just tracking it for now?",
    }

    def reply(self, message: str, profile: ImplicitProfile, history: List[Dict]) -> str:
        opener_options = self._OPENERS.get(profile.tone_label, self._OPENERS["neutral"])
        opener = opener_options[profile.turn_count % len(opener_options)]

        followup = None
        for topic in profile.dominant_topics:
            if topic in self._TOPIC_FOLLOWUPS:
                followup = self._TOPIC_FOLLOWUPS[topic]
                break

        body = f"Thanks for sharing that - \"{message.strip()[:80]}\" is useful context."
        if followup:
            return f"{opener} {body} {followup}"
        return f"{opener} {body} Tell me a bit more so I can tailor things to you."


class _OpenAIGenerator:
    def __init__(self, model: str = "gpt-4o-mini"):
        from openai import OpenAI  # imported lazily so the offline path never needs it
        self._client = OpenAI()
        self._model = model

    def reply(self, message: str, profile: ImplicitProfile, history: List[Dict]) -> str:
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(profile_brief=profile.as_prompt_fragment())
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-6:])
        messages.append({"role": "user", "content": message})
        completion = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=0.7,
            max_tokens=180,
        )
        return completion.choices[0].message.content.strip()


class _AnthropicGenerator:
    def __init__(self, model: str = "claude-3-5-haiku-20241022"):
        import anthropic  # imported lazily
        self._client = anthropic.Anthropic()
        self._model = model

    def reply(self, message: str, profile: ImplicitProfile, history: List[Dict]) -> str:
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(profile_brief=profile.as_prompt_fragment())
        messages = list(history[-6:]) + [{"role": "user", "content": message}]
        response = self._client.messages.create(
            model=self._model,
            system=system_prompt,
            messages=messages,
            max_tokens=220,
        )
        return "".join(block.text for block in response.content if hasattr(block, "text")).strip()


class ChatEngine:
    """Picks a back-end at start-up and exposes a single `reply` method."""

    def __init__(self):
        self._backend_name, self._backend = self._select_backend()

    def _select_backend(self):
        if os.getenv("OPENAI_API_KEY"):
            try:
                return "openai", _OpenAIGenerator()
            except Exception:
                logger.warning(
                    "OPENAI_API_KEY is set but the OpenAI backend failed to initialize; "
                    "falling back to the offline template generator.", exc_info=True,
                )
        if os.getenv("ANTHROPIC_API_KEY"):
            try:
                return "anthropic", _AnthropicGenerator()
            except Exception:
                logger.warning(
                    "ANTHROPIC_API_KEY is set but the Anthropic backend failed to initialize; "
                    "falling back to the offline template generator.", exc_info=True,
                )
        return "offline-template", _OfflineGenerator()

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def reply(self, message: str, profile: ImplicitProfile, history: List[Dict]) -> str:
        try:
            return self._backend.reply(message, profile, history)
        except Exception:
            # Graceful degradation if the API call fails mid-session. The
            # underlying exception (which can contain request details or
            # provider-side error text) is logged server-side only - it is
            # never appended to the user-facing reply.
            logger.warning(
                "Live model call failed for backend=%s; falling back to the offline template.",
                self._backend_name, exc_info=True,
            )
            fallback = _OfflineGenerator()
            return fallback.reply(message, profile, history)
