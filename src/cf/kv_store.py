"""
Cloudflare KV-backed profile storage, used only when this app is deployed
as a Cloudflare Worker (see src/worker.py, wrangler.jsonc).

Why this exists: Workers have no persistent local filesystem - each request
can run in a fresh isolate, so `FileProfileStore` (profiler.py, the default
used by `python app.py` / gunicorn) does not work there. This class
implements the same `ProfileStore` interface on top of a KV namespace
binding instead.

Revision note: `env` (bindings, vars) is exposed to Flask through
`request.environ["workers.env"]` - confirmed against Cloudflare's own
documented Flask example
(developers.cloudflare.com/workers/languages/python/packages/flask/, which
shows `request.environ["workers.env"].ASSETS`). An earlier version of this
file guessed the key was `"env"` (unconfirmed at the time) and additionally
carried a `bind()`/`_last_bound_env` fallback for a custom WorkerEntrypoint
subclass in src/worker.py that manually forwarded `self.env`; that
subclass turned out to be based on a different wrong guess (see
src/worker.py's revision note) and has been removed, which makes the
`bind()` fallback path dead code - removed here too, since
`request.environ["workers.env"]` is unconditionally correct on Workers.
"""

from __future__ import annotations

from typing import Any, Optional

from profiler import ProfileStore


def _get_env() -> Optional[Any]:
    from flask import request
    return request.environ.get("workers.env")


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

    async def load_raw(self, user_id: str) -> dict | None:
        import json
        # Cloudflare KV get/put/delete all return a Promise and MUST be
        # awaited (confirmed against Cloudflare's KV and Python Workers
        # docs) - this was a real bug in an earlier version of this file:
        # calling these without `await` would return an un-awaited
        # Promise/coroutine object instead of the actual value.
        raw = await self._kv().get(f"profile:{user_id}")
        if raw is None:
            return None
        return json.loads(raw)

    async def save_raw(self, user_id: str, data: dict) -> None:
        import json
        await self._kv().put(f"profile:{user_id}", json.dumps(data))

    async def delete(self, user_id: str) -> None:
        await self._kv().delete(f"profile:{user_id}")

    async def list_user_ids(self) -> list[str]:
        """Paginates through env.PROFILES_KV.list() (confirmed signature
        and response shape against developers.cloudflare.com/kv/api/
        list-keys/: {"keys": [{"name": ...}, ...], "list_complete": bool,
        "cursor": str}) and strips the "profile:" prefix each key was
        stored under. Used only by /admin/stats (app.py) - not on any
        request-handling hot path."""
        kv = self._kv()
        prefix = "profile:"
        user_ids: list[str] = []
        cursor = None
        while True:
            options = {"prefix": prefix}
            if cursor:
                options["cursor"] = cursor
            result = await kv.list(options)
            for key in result["keys"]:
                name = key["name"]
                if name.startswith(prefix):
                    user_ids.append(name[len(prefix):])
            if result["list_complete"]:
                break
            cursor = result["cursor"]
            if not cursor:
                break
        return user_ids
