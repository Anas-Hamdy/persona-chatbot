# Deploying to Cloudflare Workers

## Revision note

An earlier version of this integration had two confirmed bugs, caught on
review and fixed here: KV `get`/`put`/`delete` were called without `await`
(Cloudflare's KV bindings return a Promise and the call is a no-op without
it — confirmed against
[Cloudflare's Python Workers bindings docs](https://developers.cloudflare.com/workers/languages/python/ffi/)
and [KV write docs](https://developers.cloudflare.com/kv/api/write-key-value-pairs/)),
and `src/worker.py` never actually called `cf.kv_store.bind(env)`, so its
one fallback path for reaching `env` was dead code. Both are fixed below;
the sections below also cover a third, related gap that hadn't been
addressed yet: `os.getenv()` at Python module import time cannot see
Cloudflare's `vars`/`secrets` at all, because on Workers this module runs
once at cold start, before any request (and therefore any `env`) exists.

A fourth, separate bug showed up only once a real Cloudflare Workers Builds
deployment was attempted (Git-integration auto-deploy, not `wrangler
deploy` from a CLI): the build log showed Cloudflare running
`pip install -r requirements.txt` and then hanging for 10+ minutes trying
to build `scikit-learn` from source, before failing outright. The repo root
had both `requirements.txt` (gunicorn/local-dev deps: scikit-learn, nltk,
gunicorn - none of it Pyodide-installable) and `pyproject.toml` (the actual
Pyodide-compatible deps for this Worker), and Workers Builds' own build step
picked up `requirements.txt` instead. Confirmed against
[Cloudflare's Python packages docs](https://developers.cloudflare.com/workers/languages/python/packages/)
(Python Workers are managed via `pyproject.toml`) and Cloudflare's own
[python-workers-examples](https://github.com/cloudflare/python-workers-examples)
repo, where every example (including `flask-todo`) keeps only
`pyproject.toml` at the Worker's root - never a sibling `requirements.txt`.
Fixed by moving `requirements.txt`/`requirements-dev.txt` into `local/`, so
`pyproject.toml` is the only Python dependency manifest at the repo root.

## Second revision note (after a real deploy attempt)

Getting an actual `pywrangler deploy` to succeed surfaced two more real,
confirmed bugs beyond the KV/env ones above, neither of which could have
been caught without attempting a live deploy:

1. **`npx wrangler deploy` vs `uv run pywrangler deploy`.** Plain `wrangler`
   built this project's `pyproject.toml` into a wheel and deployed it, but
   consistently failed with `ModuleNotFoundError: No module named 'workers'`
   - a real, still-open Cloudflare bug
   ([cloudflare/workers-sdk#15208](https://github.com/cloudflare/workers-sdk/issues/15208))
   that a Cloudflare Workers Paid plan upgrade did NOT fix for this account.
   Switching to `uv run pywrangler deploy` (after `uv sync` installs
   `workers-py` and `workers-runtime-sdk` from the `dev` dependency group,
   matching Cloudflare's own documented `pyproject.toml` for Flask:
   [developers.cloudflare.com/workers/languages/python/packages/flask/](https://developers.cloudflare.com/workers/languages/python/packages/flask/))
   resolved this completely - `pywrangler` vendors the actual Python Workers
   runtime SDK into the deployed bundle; plain `wrangler` does not.
   `disable_python_external_sdk` (the other workaround mentioned in that
   GitHub issue) was tried and rejected: it removes `wsgi` from the
   `workers` package entirely, which this Flask-based app depends on.
2. **First-party module layout.** `app.py`/`chatbot_engine.py`/`profiler.py`/
   `cf/` used to live at the repo root with a `sys.path.insert()` hack in
   `src/worker.py` to reach them - this worked for local Python (which
   doesn't care where a file physically sits, only what's on `sys.path`),
   but Cloudflare's Python Workers bundler does not do a repo-wide import
   scan; per
   [developers.cloudflare.com/workers/languages/python/basics/](https://developers.cloudflare.com/workers/languages/python/basics/),
   it only auto-discovers local modules that sit in the **same directory as
   the main entrypoint**. This produced a real
   `ModuleNotFoundError: No module named 'cf'` at deploy time (right after
   the `workers` SDK bug above was fixed, so it was only reachable once that
   first bug was out of the way). Fixed by moving all of them into `src/`
   alongside `worker.py`, and updating the gunicorn/local dev path
   (`Procfile`'s `--chdir src`, `app.py`'s `static_folder="../static"`,
   `README.md`'s quickstart, `tests/test_app.py`'s `sys.path` line,
   `evaluate.py`'s import shim) so both deployment targets keep working from
   one shared source tree instead of duplicating these files.

## Architecture change from the gunicorn deployment

Cloudflare Workers are stateless, request-scoped V8 isolates with Python
support via Pyodide (WebAssembly) — not a long-running server process. That
forced real changes, not just config:

1. **No persistent local filesystem.** `profiler.py` used to write each
   user's profile to a JSON file on disk. That's replaced by
   `cf/kv_store.py`, a Cloudflare KV-backed implementation of the same
   `ProfileStore` interface, selected via `STORAGE_BACKEND=kv`. The
   gunicorn deployment is untouched — it still defaults to `FileProfileStore`.
2. **KV is async.** Cloudflare KV bindings return a Promise; every call in
   `cf/kv_store.py` is now `async def` and `await`s the underlying `get`/
   `put`/`delete`. That required making the whole `ProfileStore` interface
   in `profiler.py` async (`FileProfileStore` too, even though its file I/O
   has nothing to actually await — it's `async def` only so both backends
   share one interface), and `app.py`'s `/api/chat` and `/api/reset` routes
   are now `async def`, using Flask 3's built-in async-view support (needs
   `asgiref`, added to both `requirements.txt` and `pyproject.toml`).
3. **`env` is only reachable per-request, not at import time.** `app.py`
   used to read `SECRET_KEY`/`STORAGE_BACKEND` once via `os.getenv()` at
   module load. On Workers that line runs before any `env` exists, so it
   would always miss wrangler.jsonc's values. `app.py` now has a
   `before_request` hook, `_apply_cloudflare_env_overrides()`, that runs
   once on the first real request and re-reads `SECRET_KEY`/
   `STORAGE_BACKEND`/`KV_BINDING_NAME` from `env` if one is found —
   swapping in `KVProfileStore` and the real `SECRET_KEY` at that point.
   Verified locally with a fake `env` via Flask's test client
   (`environ_overrides={"env": FakeEnv()}`); see the note below for the one
   part of this that a live deploy is still needed to confirm.
4. **`src/worker.py` now actually wires `env` through.** It defines its own
   `WorkerEntrypoint` subclass that calls `cf.kv_store.bind(self.env)`
   before delegating to `wsgi.entrypoint(flask_app)`, instead of assigning
   that helper straight to `Default` (which never gave `bind()` a chance to
   run at all).
5. **No NLTK data download.** `vader_lexicon` needs a one-time download to
   a writable, persistent location; Workers has neither. The Workers deploy
   simply never installs `nltk` (see `pyproject.toml`) and runs on
   `profiler.py`'s existing built-in keyword-lexicon fallback.
6. **LLM backends are opt-in, not default.** The OpenAI/Anthropic Python
   SDKs make synchronous HTTP calls; only asynchronous HTTP clients are
   confirmed supported on Workers. `chatbot_engine.py` skips the LLM
   backends by default under `STORAGE_BACKEND=kv` unless
   `ALLOW_LLM_ON_WORKERS=1` is set.

## Resolved only by a real live deploy (kept for the record)

Both of these were originally flagged as unconfirmable from outside a real
Cloudflare account, and both were in fact wrong in their original form -
confirmed and fixed only once `wrangler tail` showed a real traceback:

1. **How `env` reaches Flask.** Originally guessed as
   `request.environ["env"]`; confirmed wrong and fixed to
   `request.environ["workers.env"]` against Cloudflare's own documented
   Flask example
   (developers.cloudflare.com/workers/languages/python/packages/flask/,
   which shows `request.environ["workers.env"].ASSETS`). See
   `apply_cloudflare_env_overrides()` in `app.py` and `_get_env()` in
   `cf/kv_store.py`.
2. **Whether `await`ing a KV Promise works inside Flask's built-in
   async-view support.** It does not. `/api/chat` and `/api/reset` were
   originally `async def`, relying on Flask 3's async-view support
   (`asgiref.sync.async_to_sync`) to bridge them - the normal approach
   under gunicorn, where no event loop is already running in the sync WSGI
   worker thread. On Cloudflare Workers that assumption is false: Python
   Workers' `on_fetch` is itself async and already runs inside Pyodide's
   own event loop, so `asgiref`'s `AsyncToSync` fails as soon as it
   detects one, with:
   ```
   RuntimeError: You cannot use AsyncToSync in the same thread as an
   async event loop - just await the async function directly.
   ```
   confirmed via a live deploy and `wrangler tail`. Fixed per Cloudflare's
   own documented pattern for Flask on Python Workers (same URL as above,
   "Serve a frontend" example, which bridges `ASSETS.fetch()` the same
   way): `/api/chat` and `/api/reset` are now plain synchronous `def`
   views, and the one awaitable call each needs
   (`profiler.update()`/`profiler.delete()`) is bridged through a small
   `_run_async()` helper in `app.py` - `pyodide.ffi.run_sync(coro)` on
   Workers (built for running an awaitable to completion from a call stack
   entered via an async Python function, which is exactly this situation),
   or plain `asyncio.run(coro)` everywhere else, where there is no
   already-running loop to conflict with.

## Prerequisites

- A Cloudflare account (Workers Paid plan if you need more than KV's free
  tier, but the free tier is enough to test this).
- Node.js 18+ (for `npx wrangler`) and `uv` (for `pywrangler`,
  Cloudflare's Python-specific Wrangler wrapper).

## Files added for this deployment

| File | Purpose |
|---|---|
| `wrangler.jsonc` | Worker name, entrypoint, KV binding, static assets, non-secret vars |
| `pyproject.toml` | Pyodide-compatible dependency list (Flask only — see above) |
| `src/worker.py` | Wraps `app.py`'s Flask app with Cloudflare's WSGI bridge |
| `cf/kv_store.py` | KV-backed `ProfileStore` implementation |
| `.dev.vars.example` | Template for local secrets (`pywrangler dev`) |

## Deployment commands

See the chat response for the exact sequence — summarized here for
reference:

```bash
npm install -g wrangler
uvx --from workers-py pywrangler --version   # confirms uv + pywrangler are usable
wrangler login

npx wrangler kv namespace create PROFILES_KV
# copy the returned id into wrangler.jsonc -> kv_namespaces[0].id

wrangler secret put SECRET_KEY
# paste: python -c "import secrets; print(secrets.token_hex(32))"

cp .dev.vars.example .dev.vars   # fill in SECRET_KEY for local dev only

uv run pywrangler dev             # local test at the printed localhost URL
uv run pywrangler deploy          # ships it
```
