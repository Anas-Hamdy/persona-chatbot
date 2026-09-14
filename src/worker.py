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

from workers import wsgi  # provided by the Workers Python runtime

from app import app as flask_app  # noqa: E402  (import after sys.path fix)

Default = wsgi.entrypoint(flask_app)
