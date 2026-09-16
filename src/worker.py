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

from workers import WorkerEntrypoint, wsgi  # provided by the Workers Python runtime

# app.py, chatbot_engine.py, profiler.py and cf/ all live right next to this
# file in src/ - Cloudflare's Python Workers bundler only auto-discovers
# first-party local modules that sit alongside the main entrypoint (see
# developers.cloudflare.com/workers/languages/python/basics/), not modules
# elsewhere in the repo. An earlier layout kept them at the repo root with a
# sys.path.insert() hack to make local/gunicorn imports work; that worked
# for local Python but not for what Cloudflare's own bundler scans, and
# produced a real "ModuleNotFoundError: No module named \'cf\'" at deploy
# time. This directory layout is now the single source of truth for both
# the gunicorn deployment (see Procfile's --chdir src) and this Worker.

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
