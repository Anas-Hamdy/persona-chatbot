"""
Smoke tests for the Flask API surface.

These formalize the manual checks run during the deployment-readiness pass
(see docs/DEPLOYMENT_CHECKLIST.md, item 4): a normal chat turn, a malformed
request body, an empty message, an over-length message, reset, an unknown
route, and the health check. They exercise the offline-template backend
only (no network calls, no API keys required), which is what runs by
default and in CI.

Run:
    pytest
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

TEST_PROFILE_DIR = Path(__file__).resolve().parent / "_test_profiles"


@pytest.fixture()
def client():
    os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
    os.environ["PROFILE_STORAGE_DIR"] = str(TEST_PROFILE_DIR)
    # Reimport fresh each test run so module-level state (the profiler's
    # storage_dir, the engine) picks up the env vars set above.
    for mod in ("app", "profiler", "chatbot_engine"):
        sys.modules.pop(mod, None)
    import app as app_module

    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c

    shutil.rmtree(TEST_PROFILE_DIR, ignore_errors=True)


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_chat_normal_turn(client):
    resp = client.post("/api/chat", json={"message": "I love hiking and Python programming!"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert "reply" in body and body["reply"]
    assert body["profile"]["turn_count"] == 1
    assert "technology" in body["profile"]["dominant_topics"]


def test_chat_rejects_non_json_body(client):
    resp = client.post("/api/chat", data="not json", content_type="text/plain")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_chat_rejects_empty_message(client):
    resp = client.post("/api/chat", json={"message": ""})
    assert resp.status_code == 400


def test_chat_rejects_oversized_message(client):
    resp = client.post("/api/chat", json={"message": "x" * 5000})
    assert resp.status_code == 400


def test_reset_clears_history(client):
    client.post("/api/chat", json={"message": "hello there"})
    resp = client.post("/api/reset")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_unknown_route_returns_json_404(client):
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    assert "error" in resp.get_json()
