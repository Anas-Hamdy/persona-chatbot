# Personalized Chatbot — Implicit User Profiling

[![CI](https://github.com/YOUR_USERNAME/persona-chatbot/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_USERNAME/persona-chatbot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

A Flask chatbot that builds a personalization profile from *how someone
writes* — sentiment, formality, topic, punctuation energy — instead of
asking them to fill in a form. The profile updates after every message with
a confidence-decayed weighted average, and is fed back into the reply
generator so tone and follow-up questions track the person on the other
side of the conversation.

This is the reference implementation for the thesis *"Chatbot Per Person:
Creating Personalized Chatbots based on Implicit User Profiles"* (Arab
Academy for Science, Technology and Maritime Transport). It's a working
prototype, not a hardened product — see [Limitations](#limitations--roadmap)
below and `docs/DEPLOYMENT_CHECKLIST.md` for the honest gap list before
using it with real users at scale.

## Contents

- [How it works](#how-it-works)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [API reference](#api-reference)
- [Running tests](#running-tests)
- [Deploying](#deploying)
- [Project structure](#project-structure)
- [Limitations & roadmap](#limitations--roadmap)
- [Citation](#citation)
- [License](#license)

## How it works

```
 user message
      │
      ▼
┌─────────────┐     confidence-decayed EMA      ┌──────────────────┐
│ profiler.py │ ───────────────────────────────▶ │ ImplicitProfile  │
│ (per turn)  │   sentiment · formality · topic  │ (per user_id)    │
└─────────────┘   · punctuation energy           └────────┬─────────┘
                                                            │
                                                            ▼
                                                  ┌───────────────────┐
                                                  │ chatbot_engine.py │
                                                  │ profile-conditioned│
                                                  │ reply generation  │
                                                  └────────┬───────────┘
                                                            │
                                     LLM system prompt   or offline templates
                                     (OpenAI / Anthropic)   (no API key needed)
                                                            │
                                                            ▼
                                                    reply to the user
```

- **`profiler.py`** extracts sentiment (VADER, with a small built-in
  lexicon fallback), formality, topic interest, and "energy" from each
  message, then blends each into a running profile with a weight that
  decays as more turns arrive — early messages move the profile a lot,
  later ones only nudge it.
- **`chatbot_engine.py`** turns that profile into a short natural-language
  brief and either injects it into an LLM system prompt (if
  `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is set) or drives a transparent
  template generator (no key required, so the whole system is runnable and
  demonstrable offline).
- **`app.py`** is the Flask server: a two-route JSON API plus a minimal chat
  UI with a live profile panel, useful for demoing how the profile evolves
  turn by turn.

## Quickstart

Requires Python 3.11+.

```bash
git clone https://github.com/YOUR_USERNAME/persona-chatbot.git
cd persona-chatbot
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -c "import nltk; nltk.download('vader_lexicon')"   # one-time, improves sentiment accuracy
cp .env.example .env    # optional locally; edit if you want a stable SECRET_KEY or an LLM key
python app.py
```

Open **http://localhost:5000**. No API key is required — without one, the
app uses the offline template generator so the profiling behavior can
still be observed end to end. To use a real language model, set
`OPENAI_API_KEY` or `ANTHROPIC_API_KEY` before starting the server.

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SECRET_KEY` | **Yes, in production** | random per-process | Flask session signing key. Must be set explicitly once you run more than one worker or need sessions to survive a restart. |
| `FLASK_ENV` | No | `development` | Set to `production` to enforce `SECRET_KEY`, enable secure cookies, and disable the debugger. |
| `OPENAI_API_KEY` | No | unset | Enables the OpenAI reply backend (`gpt-4o-mini`). |
| `ANTHROPIC_API_KEY` | No | unset | Enables the Anthropic reply backend (`claude-3-5-haiku`). Tried only if `OPENAI_API_KEY` is absent. |
| `PORT` | No | `5000` | Port the server listens on. |
| `PROFILE_STORAGE_DIR` | No | `profiles` | Directory where per-user profile JSON files are written. |
| `LOG_LEVEL` | No | `INFO` | Python logging level. |

## API reference

| Method & path | Body | Response |
|---|---|---|
| `GET /` | — | Chat UI (HTML) |
| `GET /healthz` | — | `{"status": "ok", "backend": "..."}` — for load balancers / uptime checks |
| `POST /api/chat` | `{"message": "..."}` | `{"reply", "profile", "profile_brief", "backend"}` |
| `POST /api/reset` | — | Clears the current session's chat history and saved profile |

`POST /api/chat` returns `400` for an empty message, a message over 4000
characters, or a non-JSON body; `500` for an unhandled server error (logged
server-side, never exposed to the client).

## Running tests

```bash
pip install -r requirements-dev.txt
pytest -v
```

The suite exercises the API surface against the offline backend only (no
network calls, no API key needed) — a normal chat turn, malformed JSON, an
empty message, an over-length message, reset, and an unknown route. It runs
automatically on every push via GitHub Actions (`.github/workflows/ci.yml`).

## Deploying

```bash
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
export FLASK_ENV=production
gunicorn "app:app" --bind 0.0.0.0:$PORT --workers 1 --threads 4
```

A `Procfile` is included for Heroku-style platforms (Render, Railway,
Fly.io). It's deliberately pinned to a single worker — chat history is
currently kept in memory per process, so more than one worker means
different requests from the same user can hit different workers with
different state. See `docs/DEPLOYMENT_CHECKLIST.md` for the full list of
what's handled and what isn't yet.

### Cloudflare Workers

A separate deployment target using Wrangler + Cloudflare KV instead of
gunicorn + local disk. See `docs/CLOUDFLARE_DEPLOYMENT.md` for what changes
architecturally (storage backend, sentiment fallback, LLM backend caveats)
and the exact `wrangler`/`pywrangler` commands.

## Project structure

```
persona-chatbot/
├── app.py                      # Flask app: routes, session handling, config
├── chatbot_engine.py           # Reply generation (LLM-conditioned or offline template)
├── profiler.py                 # Implicit profile extraction, update logic, storage interface
├── evaluate.py                 # Offline evaluation harness (persona-consistency, Distinct-n)
├── data/
│   └── personachat_sample.json # Small PersonaChat-style sample used by evaluate.py
├── static/                     # Chat UI assets (JS, CSS)
├── templates/
│   └── index.html              # Chat UI with a live profile panel
├── tests/
│   └── test_app.py             # API smoke tests (pytest)
├── docs/
│   ├── DEPLOYMENT_CHECKLIST.md      # Detailed production-readiness review
│   └── CLOUDFLARE_DEPLOYMENT.md     # Wrangler/Workers deployment notes
├── cf/
│   └── kv_store.py             # Cloudflare KV-backed profile storage (Workers only)
├── src/
│   └── worker.py               # Cloudflare Workers entrypoint (wraps app.py)
├── .github/workflows/ci.yml    # Runs the test suite on every push/PR
├── requirements.txt            # Runtime dependencies for local dev / gunicorn (pinned)
├── requirements-dev.txt        # + pytest, for local development/CI
├── pyproject.toml              # Pyodide-compatible dependencies for Cloudflare Workers
├── wrangler.jsonc              # Cloudflare Worker configuration
├── Procfile                    # Heroku-style start command
├── .env.example                # Documents every environment variable read (gunicorn deploy)
├── .dev.vars.example           # Local secrets template for `pywrangler dev`
├── .gitignore
├── LICENSE
└── README.md
```

`profiles/` and `eval_profiles/` are created automatically at runtime to
store per-user profile JSON files (local-disk deployments only); they're
git-ignored and not part of the repository.

## Limitations & roadmap

This is a thesis prototype, and it's honest about that. In short: chat
history and profiles live in local process memory / disk, so nothing
survives a restart or scales past one instance yet; there's no rate
limiting, authentication, or data-retention job; and the offline template
backend is intentionally simple rather than competing with a large
pretrained model on fluency. The full reasoning and priority order for
closing each gap is in `docs/DEPLOYMENT_CHECKLIST.md`.

## Citation

If you build on this work, please cite the underlying thesis rather than
this repository directly. The design follows ideas from:

- Ma, Z., Dou, Z., Zhu, Y., Zhong, H., & Wen, J.-R. (2021). *One Chatbot Per
  Person: Creating Personalized Chatbots based on Implicit User Profiles.*
  SIGIR 2021.
- David, S., Meidan, Y., Hersko, I., Varnovitzky, D., Mimran, D., Elovici,
  Y., & Shabtai, A. (2025). *ProfiLLM: An LLM-Based Framework for Implicit
  Profiling of Chatbot Users.*

## License

MIT — see [LICENSE](LICENSE).
