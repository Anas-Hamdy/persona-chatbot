"""
Cloudflare KV-backed profile storage, used only when this app is deployed
as a Cloudflare Worker (see src/worker.py, wrangler.jsonc).

Why this exists: Workers have no persistent local filesystem - each request
can run in a fresh isolate, so `FileProfileStore` (profiler.py, the default
used by `python app.py` / gunicorn) does not work there. This class
implements the same `ProfileStore` interface on top of a KV namespace
binding instead.

IMPORTANT - verify before relying on this in production: how the Workers
Python WSGI bridge (`wsgi.entrypoint`, used in src/worker.py) exposes
bindings to Flask is a beta API and has changed before. This module first
tries the documented-by-convention `flask.request.environ["env"]`; if that
key isn't present, it falls back to whatever `bind(env)` was called with
most recently (set explicitly from src/worker.py). Check
https://developers.cloudflare.com/workers/languages/python/ for the current
mechanism if profile persistence doesn't work as expected after deploying,
and update `_get_env()` below accordingly - that is the one piece of this
integration that could not be confirmed against a live deployment.
"""

from __future__ import annotations

from typing import Any, Optional

from profiler import ProfileStore

_last_bound_env: Optional[Any] = None


def bind(env: Any) -> None:
    """Call this once per request from src/worker.py with the Worker's
    `env` object, before any Flask route handler runs, as a fallback path
    for _get_env() below."""
    global _last_bound_env
    _last_bound_env = env


def _get_env() -> Optional[Any]:
    try:
        from flask import request
        env = request.environ.get("env")
        if env is not None:
            return env
    except Exception:
        pass
    return _last_bound_env


class KVProfileStore(ProfileStore):
    """Stores each user's profile as one JSON value under key
    `profile:<user_id>` in the bound KV namespace."""

    def __init__(self, binding_name: str = "PROFILES_KV"):
        self.binding_name = binding_name

    def _kv(self):
        env = _get_env()
        if env is None:
            raise RuntimeError(
                "No Workers `env` available - KVProfileStore can only be used "
                "when running as a Cloudflare Worker (see src/worker.py)."
            )
        kv = getattr(env, self.binding_name, None)
        if kv is None:
            raise RuntimeError(
                f"KV binding '{self.binding_name}' not found on env. Check the "
                f"kv_namespaces block in wrangler.jsonc matches this name."
            )
        return kv

    def load_raw(self, user_id: str) -> dict | None:
        import json
        raw = self._kv().get(f"profile:{user_id}")
        if raw is None:
            return None
        return json.loads(raw)

    def save_raw(self, user_id: str, data: dict) -> None:
        import json
        self._kv().put(f"profile:{user_id}", json.dumps(data))

    def delete(self, user_id: str) -> None:
        self._kv().delete(f"profile:{user_id}")
