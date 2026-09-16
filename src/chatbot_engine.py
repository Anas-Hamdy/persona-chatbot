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

# Same detection app.py uses for _on_workers - kept as a separate constant
# here (rather than imported from app.py) to avoid a circular import
# (app.py imports ChatEngine from this module).
_on_workers = "pyodide" in __import__("sys").modules or __import__("sys").platform == "emscripten"


SYSTEM_PROMPT_TEMPLATE = """You are a personalized conversational assistant.
Adapt your tone, vocabulary, and the examples you use to match the user
profile below, which was inferred implicitly from the conversation itself
(the user never filled in any form). Do not mention that you are using a
profile - just let the reply feel naturally suited to this person.

{profile_brief}

Keep replies concise (2-4 sentences) and natural."""

# Used only for the /admin/stats "personalized vs generic" comparison
# feature (app.py's ChatEngine.compare()) - a neutral baseline system
# prompt with the same length constraint but none of the profile
# conditioning, so a side-by-side reply pair isolates exactly what the
# implicit profile changed. Never used for the actual reply saved to a
# user's chat history - only for this on-demand comparison.
GENERIC_SYSTEM_PROMPT = """You are a helpful conversational assistant.
Keep replies concise (2-4 sentences) and natural."""


def build_system_prompt(profile: "ImplicitProfile") -> str:
    """The exact personalized system prompt a request would use - exposed
    so app.py can show it back to the user for transparency (what the
    thesis's "conditional generation" strategy actually injected), without
    needing a second API call."""
    return SYSTEM_PROMPT_TEMPLATE.format(profile_brief=profile.as_prompt_fragment())


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
    def __init__(self, api_key: str | None = None, model: str = "gpt-4o-mini"):
        from openai import OpenAI  # imported lazily so the offline path never needs it
        # Pass api_key explicitly rather than relying on the SDK's own
        # os.environ["OPENAI_API_KEY"] lookup - that lookup returns nothing
        # on Cloudflare Workers, where secrets are only reachable through
        # the per-request `env` object, never os.environ (see
        # ChatEngine._select_backend's revision note).
        self._client = OpenAI(api_key=api_key) if api_key else OpenAI()
        self._model = model

    def _complete(self, system_prompt: str, message: str, history: List[Dict]) -> str:
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

    def reply(self, message: str, profile: ImplicitProfile, history: List[Dict]) -> str:
        return self._complete(build_system_prompt(profile), message, history)

    def reply_generic(self, message: str, history: List[Dict]) -> str:
        """Same call, but with the neutral GENERIC_SYSTEM_PROMPT instead of
        the profile-conditioned one - used only by ChatEngine.compare()."""
        return self._complete(GENERIC_SYSTEM_PROMPT, message, history)


class _AnthropicGenerator:
    def __init__(self, api_key: str | None = None, model: str = "claude-3-5-haiku-20241022"):
        import anthropic  # imported lazily
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self._model = model

    def _complete(self, system_prompt: str, message: str, history: List[Dict]) -> str:
        messages = list(history[-6:]) + [{"role": "user", "content": message}]
        response = self._client.messages.create(
            model=self._model,
            system=system_prompt,
            messages=messages,
            max_tokens=220,
        )
        return "".join(block.text for block in response.content if hasattr(block, "text")).strip()

    def reply(self, message: str, profile: ImplicitProfile, history: List[Dict]) -> str:
        return self._complete(build_system_prompt(profile), message, history)

    def reply_generic(self, message: str, history: List[Dict]) -> str:
        """Same call, but with the neutral GENERIC_SYSTEM_PROMPT instead of
        the profile-conditioned one - used only by ChatEngine.compare()."""
        return self._complete(GENERIC_SYSTEM_PROMPT, message, history)


class ChatEngine:
    """Picks a back-end at start-up and exposes a single `reply` method."""

    def __init__(self):
        self._backend_name, self._backend = self._select_backend()

    def _select_backend(self, env=None):
        """Pick a reply backend.

        `env` is Cloudflare's per-request Workers `env` object (the same
        one app.py's `apply_cloudflare_env_overrides` reads for
        SECRET_KEY/STORAGE_BACKEND) - passed in only by `refresh_from_env()`
        below. It is None here at `__init__` time and on every other
        deployment (gunicorn/`python app.py`), where `os.getenv()` already
        sees real process environment variables directly.

        Revision note: this used to read STORAGE_BACKEND/OPENAI_API_KEY/
        ANTHROPIC_API_KEY/ALLOW_LLM_ON_WORKERS purely via `os.getenv()`
        inside `__init__`, which runs once at Worker cold start (module
        import time) - before any request, and therefore before any real
        `env`, exists on Cloudflare Workers. That is the exact mistake
        already fixed once for SECRET_KEY (see app.py's
        `_on_workers`/`apply_cloudflare_env_overrides` comments):
        `os.getenv()` can never see wrangler.jsonc's `vars`/secrets on
        Workers, so `on_workers` evaluated to `False` and every key lookup
        below silently returned nothing no matter what was actually
        configured - the engine always fell back to the offline generator
        on a real deploy, regardless of OPENAI_API_KEY/
        ALLOW_LLM_ON_WORKERS. Fixed by accepting the real `env` object and
        being re-invoked once per isolate via `refresh_from_env()`, called
        from `apply_cloudflare_env_overrides()` in app.py, which is the one
        place that object is actually reachable.

        On the sync-HTTP-clients question itself: Cloudflare's own
        python-workers-examples repo has a dedicated `sync-http-clients`
        example confirming `requests`/`urllib3`/`httpx.Client` (what the
        openai/anthropic SDKs use) all work directly on Python Workers -
        this used to be geniunely unverified when `ALLOW_LLM_ON_WORKERS`
        was first added; it no longer is, which is why that flag now
        defaults to being turned on in wrangler.jsonc's vars.
        """
        def _get(name: str) -> str | None:
            if env is not None:
                return getattr(env, name, None)
            return os.getenv(name)

        allow_llm = not _on_workers or _get("ALLOW_LLM_ON_WORKERS") == "1"
        if not allow_llm:
            logger.warning(
                "Running on Cloudflare Workers with ALLOW_LLM_ON_WORKERS not "
                "set to \"1\" - using the offline template generator."
            )
            return "offline-template", _OfflineGenerator()

        openai_key = _get("OPENAI_API_KEY")
        if openai_key:
            try:
                return "openai", _OpenAIGenerator(api_key=openai_key)
            except Exception:
                logger.warning(
                    "OPENAI_API_KEY is set but the OpenAI backend failed to initialize; "
                    "falling back to the offline template generator.", exc_info=True,
                )
        anthropic_key = _get("ANTHROPIC_API_KEY")
        if anthropic_key:
            try:
                return "anthropic", _AnthropicGenerator(api_key=anthropic_key)
            except Exception:
                logger.warning(
                    "ANTHROPIC_API_KEY is set but the Anthropic backend failed to initialize; "
                    "falling back to the offline template generator.", exc_info=True,
                )
        return "offline-template", _OfflineGenerator()

    def refresh_from_env(self, env) -> None:
        """Re-pick the backend using a real Cloudflare Workers `env`
        object. Call once per isolate, from app.py's
        `apply_cloudflare_env_overrides()` - the only place that object is
        reachable - after the module-import-time `_select_backend()` call
        (which cannot see it) already ran with nothing configured."""
        self._backend_name, self._backend = self._select_backend(env)

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @property
    def supports_comparison(self) -> bool:
        """Whether the current backend can run the personalized-vs-generic
        comparison (see compare() below) - true only for a real LLM
        backend (openai/anthropic), since _OfflineGenerator's replies are
        template-driven rather than a single conditioned model call, so a
        "same model, different prompt" comparison doesn't apply to it."""
        return hasattr(self._backend, "reply_generic")

    def system_prompt_for(self, profile: ImplicitProfile) -> str:
        """The exact personalized system prompt the current turn would use
        - exposed for transparency in the chat UI, at zero extra API cost
        (this does not call the model)."""
        return build_system_prompt(profile)

    def compare(self, message: str, profile: ImplicitProfile, history: List[Dict]) -> dict | None:
        """Run the SAME message through the SAME model twice - once with
        the real, profile-conditioned system prompt, once with the neutral
        GENERIC_SYSTEM_PROMPT - so the effect of the implicit profile on
        the reply is directly visible side by side, not just asserted.
        Returns None if the current backend doesn't support this (see
        supports_comparison) or if either call fails. Costs one extra LLM
        call - only invoked when the caller (app.py's /api/chat, via the
        `compare` request flag) explicitly asks for it, never on every
        turn by default.
        """
        if not self.supports_comparison:
            return None
        try:
            personalized_reply = self._backend.reply(message, profile, history)
            generic_reply = self._backend.reply_generic(message, history)
        except Exception:
            logger.warning(
                "Comparison call failed for backend=%s; skipping comparison for this turn.",
                self._backend_name, exc_info=True,
            )
            return None
        return {
            "personalized_reply": personalized_reply,
            "generic_reply": generic_reply,
            "personalized_system_prompt": build_system_prompt(profile),
            "generic_system_prompt": GENERIC_SYSTEM_PROMPT,
        }

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
