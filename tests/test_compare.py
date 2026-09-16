"""
Integration tests for the "personalized vs generic" comparison feature
(chatbot_engine.ChatEngine.compare(), app.py's /api/chat `compare` flag,
and comparison_log.FileComparisonLog).

Real OpenAI/Anthropic calls aren't reachable in CI or from most sandboxes,
so these tests swap the engine's live backend for a small fake object that
implements the same `reply` / `reply_generic` interface the real
_OpenAIGenerator/_AnthropicGenerator classes expose. That is exactly what
ChatEngine.supports_comparison / ChatEngine.compare() key off of
(`hasattr(self._backend, "reply_generic")`), so this exercises the real
branching logic in app.py and chatbot_engine.py end to end - the only thing
that's fake is the network call inside the backend.

Run:
    pytest tests/test_compare.py
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

TEST_PROFILE_DIR = Path(__file__).resolve().parent / "_test_profiles_compare"
TEST_COMPARISON_DIR = Path(__file__).resolve().parent / "_test_comparisons"


class _FakeComparisonBackend:
    """Stands in for _OpenAIGenerator/_AnthropicGenerator: returns
    deliberately different text for the personalized vs generic prompts so
    the divergence score is guaranteed to be > 0, without any network
    call."""

    def reply(self, message, profile, history):
        return "Personalized: right, tailored to your profile specifically!"

    def reply_generic(self, message, history):
        return "Hello there, general assistant response for anyone."


@pytest.fixture()
def client():
    os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
    os.environ["PROFILE_STORAGE_DIR"] = str(TEST_PROFILE_DIR)
    os.environ["COMPARISON_STORAGE_DIR"] = str(TEST_COMPARISON_DIR)
    for mod in ("app", "profiler", "chatbot_engine"):
        sys.modules.pop(mod, None)
    import app as app_module

    # Force the engine onto the fake comparison-capable backend regardless
    # of what's actually configured in this environment (no real API key
    # needed, and no real key should ever be required just to run tests).
    app_module.engine._backend_name = "fake-llm"
    app_module.engine._backend = _FakeComparisonBackend()

    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c

    shutil.rmtree(TEST_PROFILE_DIR, ignore_errors=True)
    shutil.rmtree(TEST_COMPARISON_DIR, ignore_errors=True)


def test_engine_reports_comparison_support_with_fake_llm_backend(client):
    import app as app_module
    assert app_module.engine.supports_comparison is True


def test_chat_without_compare_flag_has_no_comparison_key(client):
    resp = client.post("/api/chat", json={"message": "tell me about hiking"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert "comparison" not in body
    assert body["system_prompt"]  # shown for transparency even without compare mode


def test_chat_with_compare_flag_returns_side_by_side_replies(client):
    resp = client.post(
        "/api/chat",
        json={"message": "tell me about hiking", "compare": True},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert "comparison" in body
    comparison = body["comparison"]
    assert comparison["personalized_reply"] == "Personalized: right, tailored to your profile specifically!"
    assert comparison["generic_reply"] == "Hello there, general assistant response for anyone."
    assert "personalized_system_prompt" in comparison
    assert comparison["generic_system_prompt"]
    # The two fake replies share zero words, so divergence should be the max (1.0 / 100%).
    assert comparison["divergence_score"] == 1.0
    # The reply actually returned to the user for their chat history is
    # the *personalized* one, not the generic baseline.
    assert body["reply"] == comparison["personalized_reply"]


def test_compare_flag_persists_a_comparison_record(client):
    client.post("/api/chat", json={"message": "tell me about hiking", "compare": True})

    import app as app_module

    records = app_module._run_async(app_module.comparison_log.list_all())
    assert len(records) == 1
    record = records[0]
    assert record["message"] == "tell me about hiking"
    assert record["personalized_reply"] == "Personalized: right, tailored to your profile specifically!"
    assert record["generic_reply"] == "Hello there, general assistant response for anyone."
    assert record["divergence_score"] == 1.0
    assert record["backend"] == "fake-llm"


def test_admin_stats_shows_comparison_metrics(client):
    client.post("/api/chat", json={"message": "tell me about hiking", "compare": True})

    resp = client.get("/admin/stats")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Comparisons run" in html
    assert "personalized vs generic" in html.lower() or "Personalized vs generic" in html
