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
    if not _on_workers:
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


@app.before_request
def _apply_cloudflare_env_overrides():
    """On Cloudflare Workers, wrangler.jsonc's `vars`/`secrets` and bindings
    are only reachable through the per-request `env` object - never through
    os.getenv() at import time (see the comment above `_on_workers`). This
    runs once, on the first real request, and re-applies SECRET_KEY /
    STORAGE_BACKEND from `env` if one is found. It is a no-op (and cheap -
    one dict lookup) on every other deployment, where `environ["env"]` is
    simply never present.

    The `request.environ["env"]` lookup itself is the one part of this
    integration that could not be confirmed against Cloudflare's current
    docs (see cf/kv_store.py's module docstring and
    docs/CLOUDFLARE_DEPLOYMENT.md) - if this never fires on a real deploy,
    that lookup key is the first thing to check.
    """
    global _cf_env_checked
    if _cf_env_checked:
        return
    _cf_env_checked = True

    env = request.environ.get("env")
    if env is None:
        return

    try:
        env_secret_key = getattr(env, "SECRET_KEY", None)
        if env_secret_key:
            app.secret_key = env_secret_key
        elif os.getenv("FLASK_ENV") == "production" or getattr(env, "FLASK_ENV", None) == "production":
            logger.error(
                "Running on Cloudflare Workers with no SECRET_KEY found on `env` - "
                "sessions will use a random per-isolate key. Run: "
                "wrangler secret put SECRET_KEY"
            )

        storage_backend = getattr(env, "STORAGE_BACKEND", None)
        if storage_backend == "kv" and not isinstance(profiler.store, KVProfileStore):
            profiler.store = KVProfileStore(getattr(env, "KV_BINDING_NAME", "PROFILES_KV"))
            logger.info("Switched profile storage to Cloudflare KV based on env.STORAGE_BACKEND.")
    except Exception:
        logger.exception("Failed to apply Cloudflare env overrides; continuing with import-time config.")


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
async def chat():
    # async because ProfileStore.load/save must be awaited for the
    # Cloudflare KV backend (KV get/put/delete return Promises - see
    # profiler.py's ProfileStore docstring and cf/kv_store.py). Flask 3
    # runs async views via asgiref under gunicorn/dev server too, so this
    # works unchanged for the local/gunicorn (file-backed) deployment.
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
        profile = await profiler.update(user_id, message)
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
async def reset():
    user_id = _get_user_id()
    _HISTORY.pop(user_id, None)
    _HISTORY_LAST_SEEN.pop(user_id, None)
    await profiler.delete(user_id)
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
