"""
Cloudflare Workers entrypoint for the Flask app.

Wraps the existing Flask app (app.py, in this same src/ directory) with
Cloudflare's Python Workers WSGI bridge. Nothing about the Flask app itself
needed to know it's running on Workers - profiler.py picks its storage
backend from the STORAGE_BACKEND env var (set to "kv" in wrangler.jsonc),
and chatbot_engine.py already treats the LLM backends as opt-in there.

Local dev:   uv run pywrangler dev
Deploy:      uv run pywrangler deploy

Revision note: an earlier version of this file defined a custom
WorkerEntrypoint subclass calling `cf.kv_store.bind(self.env)` before
delegating to `_wsgi_handler(self).fetch(request)`, on the theory that
`wsgi.entrypoint(...)`'s returned object exposed a `WorkerEntrypoint`-
compatible `fetch(self, request)`. That composition was never documented
and turned out to be wrong - confirmed by a real deploy, which threw
`TypeError: WorkerEntrypoint.__init__() missing 1 required positional
argument: 'env'` on every request. Cloudflare's own documented pattern
(developers.cloudflare.com/workers/languages/python/packages/flask/) is
the simple one-line assignment below; `env` (bindings, vars) is reached
inside Flask via `request.environ["workers.env"]` instead (see
cf/kv_store.py and app.py's _apply_cloudflare_env_overrides), which made
the custom subclass's manual `bind()` plumbing unnecessary to begin with.
"""

from workers import wsgi  # provided by the Workers Python runtime

# app.py, chatbot_engine.py, profiler.py and cf/ all live right next to this
# file in src/ - Cloudflare's Python Workers bundler only auto-discovers
# first-party local modules that sit alongside the main entrypoint (see
# developers.cloudflare.com/workers/languages/python/basics/), not modules
# elsewhere in the repo. An earlier layout kept them at the repo root with a
# sys.path.insert() hack to make local/gunicorn imports work; that worked
# for local Python but not for what Cloudflare's own bundler scans, and
# produced a real "ModuleNotFoundError: No module named 'cf'" at deploy
# time. This directory layout is now the single source of truth for both
# the gunicorn deployment (see Procfile's --chdir src) and this Worker.

from app import app as flask_app, apply_cloudflare_env_overrides


def _app_with_env_setup(environ, start_response):
    # Must run before flask_app(environ, start_response) - Flask opens the
    # session as part of its own wsgi_app, before any before_request hook
    # gets a chance to run. See apply_cloudflare_env_overrides's docstring
    # (revision note 2) in app.py for the live-deploy traceback that proved
    # this ordering matters.
    apply_cloudflare_env_overrides(environ)
    return flask_app(environ, start_response)


Default = wsgi.entrypoint(_app_with_env_setup)
