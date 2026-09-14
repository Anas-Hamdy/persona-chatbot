# Deployment Checklist — Personalized Chatbot Prototype

Reviewed as a full-stack code/deployment-readiness pass. This is honest
about scope: the app started as a thesis demo, not a production service, so
this document separates what was actually broken (fixed below), from what
is a reasonable prototype shortcut that would need real work before real
users touch it.

## Already fixed in this pass

- **Hardcoded, per-process `SECRET_KEY`.** `app.secret_key` was generated
  fresh with `secrets.token_hex(16)` every time the process started. With
  more than one worker process, or any restart, every existing session
  cookie became invalid — and in a multi-worker deployment, different
  workers would have *different* keys at the same time, so the same user
  could get logged out mid-conversation depending on which worker handled
  the request. Now reads `SECRET_KEY` from the environment, refuses to
  start without one when `FLASK_ENV=production`, and only falls back to a
  temporary key (with a loud warning) for local development.
- **Debug mode / dev server exposed by default.** `app.run(debug=True)`
  leaves Werkzeug's interactive debugger reachable, which allows arbitrary
  code execution if the debug PIN is ever exposed or guessed. `debug` now
  defaults off unless `FLASK_ENV` is not `production` and `FLASK_DEBUG=1`,
  and the README/Procfile point at `gunicorn`, a real WSGI server, instead
  of the Flask dev server, for anything beyond local testing.
- **Bound to `127.0.0.1` only.** The dev server as configured couldn't
  accept traffic from outside its own container/VM. Now binds `0.0.0.0`
  and reads `PORT` from the environment, which is what every common PaaS
  (Render, Railway, Fly.io, Heroku-style dynos) expects.
- **Unhandled/ambiguous JSON parsing.** `request.get_json(force=True)`
  would raise on a malformed body instead of returning a clean 400. Now
  uses `get_json(silent=True)` and returns a proper error response; also
  added a message-length cap (4000 chars) and a global `MAX_CONTENT_LENGTH`
  so an oversized request body is rejected before it's even parsed.
- **Internal exception text leaked to end users.** When a live LLM call
  failed, the fallback reply appended the raw exception message (e.g.
  `"(note: live model call failed: <exception>)"`) directly into what the
  user sees — a real information-disclosure risk if that exception ever
  contains request/account details. It's now logged server-side only; the
  user gets a clean fallback reply with no diagnostic text attached.
- **Silent backend misconfiguration.** If `OPENAI_API_KEY`/
  `ANTHROPIC_API_KEY` was set but the client failed to initialize (bad key
  format, missing package, etc.), the code silently fell back to the
  offline template generator with no signal anywhere — you could believe
  you're running on a real LLM and actually be on canned templates. Now
  logs a warning naming exactly what failed.
- **Silent, undocumented sentiment degradation.** If the NLTK
  `vader_lexicon` corpus was never downloaded, the profiler silently used a
  much cruder 16-word built-in lexicon with no indication anywhere that
  this had happened. Now probes VADER at start-up and logs a clear warning
  with the fix (`nltk.download('vader_lexicon')`) if it's missing; README
  and requirements now mention this step explicitly.
- **Unbounded in-memory growth.** `_HISTORY` had no size limit — a very
  long single conversation, or simply many distinct users over time,
  grows this dict forever with no eviction, for the life of the process.
  Now each user's history is trimmed to the last 40 turns, and the whole
  map is capped at 5,000 tracked users with simple least-recently-used
  eviction.
- **No health-check endpoint.** Added `GET /healthz`, which almost every
  container platform, load balancer, or uptime monitor expects to poll.
- **Unpinned dependencies.** `requirements.txt` used `>=` everywhere,
  meaning a fresh `pip install` today and one six months from now can
  silently pull different, potentially breaking versions. Pinned to exact
  versions, and added `gunicorn` (was missing entirely — there was no
  production-capable WSGI server in the dependency list at all).
- **No cookie hardening.** Added `HttpOnly`, `SameSite=Lax`, and
  `Secure` (enabled only when `FLASK_ENV=production`, since `Secure`
  cookies are silently dropped over plain HTTP).
- **Wrong citations in code comments.** `profiler.py`'s module docstring
  attributed "One Chatbot Per Person" to "Zhang, Sun & Zheng, 2021" (should
  be Ma, Dou, Zhu, Zhong & Wen, SIGIR 2021) and "ProfiLLM" to "Rosenfeld et
  al., 2025" (should be David, Meidan, Hersko, Varnovitzky, Mimran, Elovici
  & Shabtai) — the same author mix-up already caught and corrected in the
  thesis paper, but it had separately made it into the code comments too.
- **Missing `.gitignore` / `.env.example` / `Procfile`.** None of these
  existed. `profiles/` (real user-derived data, even if pseudonymous)
  could have been committed by accident; there was no documented list of
  what environment variables the app actually reads; and there was no
  process file telling a PaaS how to actually start the app in production.

All of the above were verified with the Flask test client (health check,
a normal chat turn, malformed JSON, empty message, an over-length message,
reset, and an unknown route) after the fix — see the bottom of this file
for exactly what was and wasn't exercised.

## What remains before this is genuinely production-ready

These are real gaps, not nitpicks — none of them are "fixed," and they're
listed in roughly the order I'd tackle them:

1. **No shared/persistent state across processes.** Chat history lives in
   an in-memory Python dict; user profiles live as individual JSON files
   on local disk. Both assumptions break the moment you run more than one
   worker/instance (each has its own memory and, on most PaaS, its own
   ephemeral filesystem that's wiped on every deploy or restart). Before
   scaling past one process, both need to move to a shared store — Redis
   is a natural fit for chat history (short-lived, fast), and a real
   database (Postgres, SQLite-if-truly-single-instance) for profiles if
   they need to survive redeploys. The `Procfile` in this repo is
   deliberately pinned to one worker specifically because of this gap —
   don't raise it without doing this first.
2. **No rate limiting.** `/api/chat` has no per-IP or per-user request
   cap. If a real LLM key is configured, this is a direct cost-control gap
   (anyone can run up your OpenAI/Anthropic bill); even on the offline
   fallback, it's an easy DoS vector. Flask-Limiter (or a reverse-proxy
   level limit, e.g. in nginx or the platform's edge) is the standard fix.
3. **No authentication.** Every visitor gets an anonymous session cookie
   and that's the entire identity model. Fine for a public demo; not fine
   if this is meant to track real, returning users or protect their data
   from each other beyond "you'd need to steal a cookie."
4. **No automated tests.** There is no test suite — the checks in this
   pass were manual, one-off Flask-test-client calls, not a repeatable
   CI-run suite. At minimum, the four `/api/chat` edge cases exercised
   here (empty message, oversized message, malformed JSON, a normal turn)
   should become real `pytest` cases before this goes near a CI/CD
   pipeline. `evaluate.py` is a research metric script, not a test suite,
   and shouldn't be mistaken for one.
5. **No data-retention story that matches what the paper claims.** The
   thesis's ethics appendix mentions automatic deletion of inactive users'
   data; the code has no such mechanism at all — `profiles/*.json` files
   accumulate forever with no expiry job. If this is ever used with real
   participants (per `hybrid_sentiment/human_study/PROTOCOL.md`), that gap
   needs closing first, or the paper's claim needs to keep saying this is
   aspirational rather than implemented — don't let the two drift apart.
6. **No containerization.** There's a `Procfile` (Heroku-style) but no
   `Dockerfile`. Fine if the target platform is Heroku/Railway-style
   buildpacks; needed if the target is anything container-native
   (Kubernetes, ECS, Cloud Run).
7. **No structured logging/observability.** Logging now exists (it didn't
   before), but it's plain `logging.warning`/`logging.exception` to
   stdout — no request IDs, no metrics, no error-tracking integration
   (Sentry or similar). Acceptable for a low-traffic demo; not for
   anything with real users depending on uptime.
8. **CSRF is unaddressed.** The session cookie defaults (`SameSite=Lax`)
   give some baseline protection against cross-site POSTs, but there's no
   explicit CSRF token on the two POST routes. Low risk today since the
   only "sensitive" action is resetting your own demo profile, but worth
   closing before there's anything more consequential behind a POST.
9. **No secrets manager.** `.env.example` documents what's needed, but
   actual secrets (`SECRET_KEY`, any LLM API key) still have to be set by
   hand in whatever platform's dashboard. Fine at this scale; a real org
   deployment would put these in a proper secrets manager instead of
   platform-level env vars.

## What was actually tested in this pass

Using Flask's built-in test client (no network, no external services):
`GET /healthz`, a normal `POST /api/chat` turn (verified against a real
message, checked reply/profile/profile_brief shape), `POST /api/chat` with
a non-JSON body, an empty message, a message over the 4000-character cap,
`POST /api/reset`, and an unknown route (`404`). All returned the expected
status code and payload shape. This is not a substitute for a real test
suite (see item 4 above) — it's the manual equivalent of what this review
could verify without deploying anywhere.
