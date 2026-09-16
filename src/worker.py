"""
Cloudflare Workers entrypoint for the Flask app.

Wraps the existing Flask app (app.py, at the repo root) with Cloudflare's
Python Workers WSGI bridge. Nothing about the Flask app itself needed to
know it's running on Workers - profiler.py picks its storage backend from
the STORAGE_BACKEND env var (set to "kv" in wrangler.jsonc), and
chatbot_engine.py already treats the LLM backends as opt-in there.

Local dev:   uv run pywrangler dev
Deploy:      uv run pywrangler deploy

If `wsgi.entrypoint` ever changes shape in a future Workers Python release,
this is the one file that needs to change - see
https://developers.cloudflare.com/workers/languages/python/packages/flask/
for the current documented pattern.
"""

import sys
from pathlib import Path

# The Flask app and its dependencies (app.py, chatbot_engine.py, profiler.py,
# cf/) live at the repository root, one level up from src/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workers import WorkerEntrypoint, wsgi  # provided by the Workers Python runtime

import cf.kv_store as kv_store
from app import app as flask_app  # noqa: E402  (import after sys.path fix)

_wsgi_handler = wsgi.entrypoint(flask_app)


class Default(WorkerEntrypoint):
    """Manual entrypoint (instead of `Default = wsgi.entrypoint(flask_app)`
    directly) so `cf.kv_store.bind(self.env)` actually runs before Flask
    handles the request - the earlier version of this file assigned
    `wsgi.entrypoint(flask_app)` straight to `Default` and never called
    `bind()` at all, which was a real bug: KVProfileStore's `environ["env"]`
    lookup was the only path that could ever supply an `env`, with no
    fallback actually wired in.

    This still assumes `wsgi.entrypoint(...)`'s returned object exposes an
    async `fetch(self, request)` compatible with `WorkerEntrypoint` - that
    composition is not documented and could not be confirmed without a live
    deploy. If `pywrangler dev` errors on this file, the safe fallback is
    reverting to `Default = wsgi.entrypoint(flask_app)` and relying solely
    on the `environ["env"]` path in cf/kv_store.py."""

    async def fetch(self, request):
        kv_store.bind(self.env)
        return await _wsgi_handler(self).fetch(request)
