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

## Two things that still cannot be confirmed without a live deploy

Both were verified as far as they can be from outside an actual Cloudflare
account — with Flask's test client and hand-built fake `env`/KV objects —
but two specific pieces of plumbing are beta, undocumented internals that
only a real `pywrangler dev`/`deploy` run can confirm:

1. **How `env` reaches Flask at all.** `cf/kv_store.py` and the
   `before_request` hook both read `flask.request.environ.get("env")`,
   which is the conventional way a WSGI bridge exposes platform objects to
   a framework that only knows about `environ` — but Cloudflare's docs
   don't spell out that Python Workers' WSGI server does this. If profile
   data isn't persisting after a real deploy, print
   `dict(request.environ)` inside a route to see what keys Cloudflare's
   bridge actually injects, and adjust `_get_env()` accordingly — that
   function is intentionally the one place this assumption lives.
2. **Whether `await`ing a KV Promise works inside Flask's async-view event
   loop.** Flask runs `async def` views by bridging them through `asgiref`
   (`async_to_sync`), which can spin up its own asyncio event loop. Pyodide
   resolves JS Promises through its own event loop (`pyodide.webloop`).
   Whether those two compose correctly — i.e., whether `await
   self.env.PROFILES_KV.get(...)` resolves correctly from inside an
   asgiref-managed loop on this specific runtime — is not documented
   anywhere found, and is the single highest-risk unknown in this
   integration. Test a real chat turn against `pywrangler dev` and confirm
   the profile actually appears in KV (`wrangler kv key list --binding
   PROFILES_KV`) before trusting this in production. If it doesn't compose,
   the fix is to bypass Flask's async view for this one code path — e.g.
   read/write KV directly in `src/worker.py`'s own `fetch()` (which already
   runs on Pyodide's native loop) and pass the loaded profile into the WSGI
   call via `environ`, rather than awaiting from inside the Flask view.

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
