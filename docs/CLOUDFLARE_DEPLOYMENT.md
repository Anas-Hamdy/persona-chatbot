# Deploying to Cloudflare Workers

## Architecture change from the gunicorn deployment

Cloudflare Workers are stateless, request-scoped V8 isolates with Python
support via Pyodide (WebAssembly) — not a long-running server process. That
forced three real changes, not just config:

1. **No persistent local filesystem.** `profiler.py` used to write each
   user's profile to a JSON file on disk. That's replaced by
   `cf/kv_store.py`, a Cloudflare KV-backed implementation of the same
   `ProfileStore` interface, selected via `STORAGE_BACKEND=kv`. The
   gunicorn deployment is untouched — it still defaults to `FileProfileStore`.
2. **No NLTK data download.** `vader_lexicon` needs a one-time download to
   a writable, persistent location; Workers has neither. The Workers deploy
   simply never installs `nltk` (see `pyproject.toml`) and runs on
   `profiler.py`'s existing built-in keyword-lexicon fallback — that
   fallback was already written for exactly this situation, it's just now
   the primary path instead of a rare degraded one.
3. **LLM backends are opt-in, not default.** The OpenAI/Anthropic Python
   SDKs make synchronous HTTP calls. Cloudflare's Python Workers docs
   confirm only *asynchronous* HTTP clients (aiohttp, httpx-async) are
   supported so far — synchronous SDK calls are unverified on this
   runtime. `chatbot_engine.py` now skips them by default when
   `STORAGE_BACKEND=kv` and uses the offline template generator, unless you
   explicitly set `ALLOW_LLM_ON_WORKERS=1` and test it yourself.

Everything else (the profiling logic, tone/topic extraction, the reply
templates, the Flask routes themselves) is unchanged — `src/worker.py` just
wraps the existing `app.py` with Cloudflare's WSGI bridge.

## One thing to verify at deploy time

`cf/kv_store.py` reads the Worker's `env` (to reach the KV binding) from
`flask.request.environ["env"]`, which is the conventional way WSGI bridges
expose platform objects — but Cloudflare's Python Workers WSGI adapter is a
beta API and this exact mechanism is not spelled out in their current
docs. If profile data isn't persisting after you deploy, check
`_get_env()` in `cf/kv_store.py` against whatever
https://developers.cloudflare.com/workers/languages/python/ documents at
the time, and update that one function — nothing else needs to change.

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
