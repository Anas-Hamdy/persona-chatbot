"""
Flask entry point for the personalized-chatbot prototype.

Local run:
    python app.py

Production run (see docs/DEPLOYMENT_CHECKLIST.md for the full picture):
    gunicorn "app:app" --bind 0.0.0.0:$PORT --workers 2

Each browser tab gets its own `user_id` (a random token stored in the
session cookie), so you can open two tabs and watch two independent
implicit profiles form side by side - useful for demonstrating the system
during a thesis defense.
"""

from __future__ import annotations

import logging
import os
import secrets
import time

from flask import Flask, jsonify, render_template, request, session

from chatbot_engine import ChatEngine
from profiler import ImplicitProfiler
from cf.kv_store import KVProfileStore  # safe to import unconditionally - no Workers-only deps at import time

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("persona_chatbot")

# static/ stays at the repo root (unchanged, since wrangler.jsonc's
# assets.directory="./static" and Workers Builds still expect it there) -
# app.py moved into src/ (see src/worker.py's comment for why), so Flask's
# default static_folder ("static" next to this file) would otherwise look
# for a nonexistent src/static/ during local/gunicorn runs.
app = Flask(__name__, static_folder="../static", static_url_path="/static")

# Cloudflare Workers (Pyodide) likely do NOT populate os.environ from
# wrangler.jsonc's `vars`/`secrets` - those are only confirmed to be
# exposed as attributes on the per-request `env` object (self.env in
# src/worker.py / WorkerEntrypoint.fetch), not as process environment
# variables. Worse, on Workers this module only runs once at cold start,
# before any request (and therefore any `env`) exists at all - so no
# amount of os.getenv() here can see wrangler.jsonc's values. `_on_workers`
# is used below to avoid hard-failing at import time for a condition
# (missing SECRET_KEY) we cannot actually check yet in that environment;
# `_apply_cloudflare_env_overrides()` (registered as a before_request hook)
# is what actually re-reads config from `env` once a real request - and
# therefore a real `env` - is available.
_on_workers = "pyodide" in __import__("sys").modules or __import__("sys").platform == "emscripten"

# SECRET_KEY must come from the environment in any deployment with more than
# one worker process or that needs sessions to survive a restart - a key
# generated fresh per process (the old `secrets.token_hex(16)` default) means
# every worker/restart invalidates every existing session cookie. Falling
# back to a random key is still fine for a single-process local demo.
_secret_key = os.getenv("SECRET_KEY")
if not _secret_key:
    if os.getenv("FLASK_ENV") == "production" and not _on_workers:
        raise RuntimeError(
            "SECRET_KEY environment variable is required when FLASK_ENV=production."
        )
    if _on_workers:
        # Cannot call secrets.token_hex()/os.urandom() here - Cloudflare
        # bans randomness while "a Worker is starting" (i.e. at module
        # import time, before any request), since a value generated now
        # would be cached and repeated identically across isolate
        # instances, which is exactly the predictability this exists to
        # prevent. Confirmed via a real deploy attempt:
        # OSError: [Errno 29] Randomness is not allowed while a Worker is
        # starting (developers.cloudflare.com/workers/platform/limits/
        # #worker-startup-time). Left unset here; _apply_cloudflare_env_
        # overrides() below (a before_request hook - runs during request
        # handling, after startup, when randomness is allowed again) sets
        # the real value from env.SECRET_KEY, or generates a random
        # fallback there if that's missing too.
        _secret_key = None
    else:
        logger.warning(
            "SECRET_KEY not set - generating a temporary one for this process. "
            "Sessions will be invalidated on restart and will NOT be shared across "
            "multiple worker processes. Set SECRET_KEY before deploying."
        )
        _secret_key = secrets.token_hex(16)
app.secret_key = _secret_key

# Reject grossly oversized request bodies before they reach json parsing.
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH_BYTES", 16_000))

# Cookie hardening - Secure requires the app to actually be served over
# HTTPS (true on every real deployment target); it is left off for local
# http://localhost development via FLASK_ENV.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_ENV") == "production"

# STORAGE_BACKEND selects where per-user profiles are persisted:
#   "file" (default) - JSON files on local disk, used by `python app.py`
#                       and the gunicorn/Procfile deployment.
#   "kv"              - Cloudflare KV, used by the Workers deployment
#                       (set via wrangler.jsonc vars; see cf/kv_store.py and
#                       docs/CLOUDFLARE_DEPLOYMENT.md - Workers has no
#                       persistent local filesystem, so "file" cannot work there).
if os.getenv("STORAGE_BACKEND", "file") == "kv":
    profiler = ImplicitProfiler(store=KVProfileStore(os.getenv("KV_BINDING_NAME", "PROFILES_KV")))
else:
    profiler = ImplicitProfiler(storage_dir=os.getenv("PROFILE_STORAGE_DIR", "profiles"))
engine = ChatEngine()

# In-memory per-user chat history. This is intentionally simple for a
# single-process demo, but it has real limits documented in
# docs/DEPLOYMENT_CHECKLIST.md: it is lost on restart, it is NOT shared
# across multiple gunicorn/uwsgi worker processes, and on Cloudflare Workers
# (STORAGE_BACKEND=kv) it only survives for as long as the same isolate
# happens to be reused between requests - it is not backed by KV, so do not
# rely on multi-turn LLM context surviving reliably in that deployment.
_HISTORY: dict[str, list[dict]] = {}
_HISTORY_LAST_SEEN: dict[str, float] = {}
_MAX_HISTORY_TURNS = 40   # 20 user+assistant pairs; bounds per-user memory growth
_MAX_TRACKED_USERS = 5000  # simple cap so an idle demo server can't grow forever


_cf_env_checked = False


def apply_cloudflare_env_overrides(environ: dict) -> None:
    """On Cloudflare Workers, wrangler.jsonc's `vars`/`secrets` and bindings
    are only reachable through the per-request `env` object - never through
    os.getenv() at import time (see the comment above `_on_workers`). This
    re-applies SECRET_KEY / STORAGE_BACKEND from `env` if one is found. It
    is a no-op (and cheap - one dict lookup) on every other deployment,
    where `environ["workers.env"]` is simply never present.

    Revision note (1): this used to read `request.environ["env"]`, an
    unconfirmed guess. Confirmed correct key against Cloudflare's own
    documented Flask example
    (developers.cloudflare.com/workers/languages/python/packages/flask/,
    which shows `request.environ["workers.env"].ASSETS`) - it's
    `"workers.env"`, not `"env"`.

    Revision note (2): this used to be a `@app.before_request` hook reading
    `flask.request.environ`. That was wrong in a way that only showed up on
    a live deploy: Flask opens the session object
    (`session_interface.open_session(...)`) as part of building the request
    context, which happens BEFORE any before_request hook runs. So by the
    time this hook set `app.secret_key`, Flask had already opened the
    session with `secret_key=None`, gotten back a NullSession, and every
    `session[...]` access for the rest of that request raised
    "RuntimeError: The session is unavailable because no secret key was
    set" - confirmed via `wrangler tail`, which showed this function's own
    "Switched profile storage to Cloudflare KV" log line succeed
    immediately before that exact exception. Fixed by calling this from a
    plain WSGI middleware in worker.py instead, wrapping flask_app so it
    runs before flask_app(environ, start_response) - i.e. before Flask
    even starts building the request context - while still running at
    request time (not import time), so secrets.token_hex() below is legal.
    """
    global _cf_env_checked
    if _cf_env_checked:
        return
    _cf_env_checked = True

    env = environ.get("workers.env")
    if env is None:
        return

    try:
        env_secret_key = getattr(env, "SECRET_KEY", None)
        if env_secret_key:
            app.secret_key = env_secret_key
        elif app.secret_key is None:
            # Import time (see _on_workers above) deliberately left this
            # unset, since generating it there would violate Cloudflare's
            # "no randomness while a Worker is starting" rule. This runs
            # during request handling instead, where secrets.token_hex()
            # is safe to call.
            app.secret_key = secrets.token_hex(16)
            if os.getenv("FLASK_ENV") == "production" or getattr(env, "FLASK_ENV", None) == "production":
                logger.error(
                    "Running on Cloudflare Workers with no SECRET_KEY found on `env` - "
                    "sessions will use a random per-isolate key that changes on every "
                    "cold start, invalidating existing sessions unpredictably. Run: "
                    "wrangler secret put SECRET_KEY"
                )

        storage_backend = getattr(env, "STORAGE_BACKEND", None)
        if storage_backend == "kv" and not isinstance(profiler.store, KVProfileStore):
            profiler.store = KVProfileStore(getattr(env, "KV_BINDING_NAME", "PROFILES_KV"))
            logger.info("Switched profile storage to Cloudflare KV based on env.STORAGE_BACKEND.")

        # Same reasoning as the SECRET_KEY/STORAGE_BACKEND overrides above:
        # ChatEngine.__init__ ran at module import time and could not see
        # env.OPENAI_API_KEY / env.ALLOW_LLM_ON_WORKERS (os.getenv() sees
        # nothing on Workers), so it always chose the offline generator.
        # This re-picks the backend now that a real `env` is available.
        engine.refresh_from_env(env)
        logger.info("Chat backend after applying Workers env: %s", engine.backend_name)
    except Exception:
        logger.exception("Failed to apply Cloudflare env overrides; continuing with import-time config.")


def _run_async(coro):
    """Bridge from Flask's synchronous view functions to this app's async
    ProfileStore interface (see profiler.py's ProfileStore docstring).

    Flask views here are deliberately plain `def`, not `async def`. An
    earlier version made /api/chat and /api/reset `async def` and relied on
    Flask 3's built-in async-view support (asgiref.sync.async_to_sync) to
    bridge them - that is the documented approach for a normal WSGI server
    (gunicorn), but it broke on a live Cloudflare Workers deploy:
    RuntimeError: "You cannot use AsyncToSync in the same thread as an
    async event loop - just await the async function directly." Root
    cause: Python Workers' on_fetch is itself async and already runs
    inside Pyodide's own event loop, so asgiref's AsyncToSync (which
    assumes it is being called from a plain sync thread with no running
    loop) fails as soon as it detects one already running.

    Fixed per Cloudflare's own documented pattern for Flask on Python
    Workers (developers.cloudflare.com/workers/languages/python/packages/
    flask/, "Serve a frontend" example, which bridges ASSETS.fetch() the
    same way): keep views synchronous, and bridge the one awaitable call
    each needs with `pyodide.ffi.run_sync`, which is built for exactly
    this - running an awaitable to completion from inside a call stack
    that was entered via an async Python function (true here, since
    Workers' WSGI bridge calls this synchronous app from its own async
    on_fetch). Off Workers (gunicorn/`python app.py`), there is no
    already-running loop in a sync WSGI worker thread, so plain
    asyncio.run() works instead.
    """
    if _on_workers:
        from pyodide.ffi import run_sync
        return run_sync(coro)
    import asyncio
    return asyncio.run(coro)


def _get_user_id() -> str:
    if "user_id" not in session:
        session["user_id"] = secrets.token_hex(8)
    return session["user_id"]


def _touch_history(user_id: str) -> list[dict]:
    """Return this user's history, trimming old turns and evicting the
    least-recently-used user if the in-memory map has grown too large."""
    history = _HISTORY.setdefault(user_id, [])
    _HISTORY_LAST_SEEN[user_id] = time.time()

    if len(_HISTORY) > _MAX_TRACKED_USERS:
        oldest_user = min(_HISTORY_LAST_SEEN, key=_HISTORY_LAST_SEEN.get)
        if oldest_user != user_id:
            _HISTORY.pop(oldest_user, None)
            _HISTORY_LAST_SEEN.pop(oldest_user, None)

    return history


@app.route("/")
def index():
    user_id = _get_user_id()
    return render_template("index.html", user_id=user_id, backend=engine.backend_name)


@app.route("/healthz")
def healthz():
    """Liveness/readiness probe for load balancers and container platforms."""
    return jsonify({"status": "ok", "backend": engine.backend_name}), 200


@app.route("/api/chat", methods=["POST"])
def chat():
    # Deliberately sync - see _run_async's docstring for why `async def`
    # here breaks on Cloudflare Workers.
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "request body must be valid JSON"}), 400

    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "empty message"}), 400
    if len(message) > 4000:
        return jsonify({"error": "message too long (max 4000 characters)"}), 400

    user_id = _get_user_id()
    history = _touch_history(user_id)

    try:
        profile = _run_async(profiler.update(user_id, message))
        reply = engine.reply(message, profile, history)
    except Exception:
        logger.exception("Unhandled error while generating a reply for user %s", user_id)
        return jsonify({"error": "something went wrong generating a reply, please try again"}), 500

    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    del history[:-_MAX_HISTORY_TURNS]

    return jsonify({
        "reply": reply,
        "profile": profile.to_dict(),
        "profile_brief": profile.as_prompt_fragment(),
        "backend": engine.backend_name,
    })


@app.route("/api/reset", methods=["POST"])
def reset():
    # Deliberately sync - see _run_async's docstring.
    user_id = _get_user_id()
    _HISTORY.pop(user_id, None)
    _HISTORY_LAST_SEEN.pop(user_id, None)
    _run_async(profiler.delete(user_id))
    return jsonify({"status": "ok"})


@app.errorhandler(404)
def not_found(_err):
    return jsonify({"error": "not found"}), 404


@app.errorhandler(500)
def server_error(_err):
    return jsonify({"error": "internal server error"}), 500


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "1" if os.getenv("FLASK_ENV") != "production" else "0") == "1"
    port = int(os.getenv("PORT", 5000))
    # host="0.0.0.0" is required for the app to be reachable at all inside a
    # container or PaaS dyno (default 127.0.0.1 only accepts local traffic).
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
