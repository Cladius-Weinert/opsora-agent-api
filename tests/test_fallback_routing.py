"""Tests for opsora_server fallback routing (mocked providers, no network).

Covers: primary success, primary-fail -> fallback success, all-fail error
shape, unknown-model passthrough, and the WARNING logs emitted on failure.
"""

import logging

import opsora_server


def _ok(body_model="m"):
    return ({"choices": [{"message": {"content": "ok"}}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1}}, 200)


def _err(status, msg="boom"):
    return ({"error": {"message": msg, "type": "upstream_error"}}, status)


class FakeProvider:
    """Records calls and replays canned (result, status) pairs in order."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []  # list of (provider_name, model_in_body)

    def __call__(self, provider_name, endpoint, body, timeout=120):
        self.calls.append((provider_name, body.get("model")))
        return self.outcomes.pop(0)


class TestRouteWithFallback:
    def test_primary_success_no_fallback_attempted(self, monkeypatch):
        fake = FakeProvider([_ok()])
        monkeypatch.setattr(opsora_server, "_call_provider", fake)

        body = {"model": "opsora-fast", "messages": []}
        result, status, provider, real_model = opsora_server._route_with_fallback(
            "opsora-fast", "/chat/completions", body
        )
        assert status == 200
        assert provider == "nvidia"
        assert real_model == "deepseek-ai/deepseek-v4-flash"
        assert len(fake.calls) == 1
        # body must be rewritten to the real upstream model
        assert body["model"] == "deepseek-ai/deepseek-v4-flash"

    def test_primary_fails_fallback_succeeds(self, monkeypatch, caplog):
        fake = FakeProvider([_err(429, "rate limited upstream"), _ok()])
        monkeypatch.setattr(opsora_server, "_call_provider", fake)

        body = {"model": "opsora-fast", "messages": []}
        with caplog.at_level(logging.WARNING, logger="opsora"):
            result, status, provider, real_model = opsora_server._route_with_fallback(
                "opsora-fast", "/chat/completions", body
            )
        assert status == 200
        assert provider == "nvidia"
        assert real_model == "stepfun-ai/step-3.7-flash"
        assert fake.calls == [
            ("nvidia", "deepseek-ai/deepseek-v4-flash"),
            ("nvidia", "stepfun-ai/step-3.7-flash"),
        ]
        # Failure must be logged with status + reason before falling back
        warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1
        assert "429" in warns[0]
        assert "rate limited upstream" in warns[0]
        assert "deepseek-ai/deepseek-v4-flash" in warns[0]

    def test_all_attempts_fail_returns_last_error_shape(self, monkeypatch, caplog):
        fake = FakeProvider([_err(429, "primary limited"),
                             _err(500, "fb one broken"),
                             _err(503, "fb two down")])
        monkeypatch.setattr(opsora_server, "_call_provider", fake)

        body = {"model": "opsora-fast", "messages": []}
        with caplog.at_level(logging.WARNING, logger="opsora"):
            result, status, provider, real_model = opsora_server._route_with_fallback(
                "opsora-fast", "/chat/completions", body
            )
        # Last failure wins, attributed to the primary provider/model
        assert status == 503
        assert provider == "nvidia"
        assert real_model == "deepseek-ai/deepseek-v4-flash"
        assert result["error"]["message"] == "fb two down"
        assert len(fake.calls) == 3
        # Every failed attempt is logged
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 3
        assert "no more fallbacks" in warns[-1].getMessage()

    def test_unknown_model_passes_through_to_nvidia(self, monkeypatch):
        fake = FakeProvider([_ok()])
        monkeypatch.setattr(opsora_server, "_call_provider", fake)

        body = {"model": "meta/llama-3.1-8b-instruct", "messages": []}
        result, status, provider, real_model = opsora_server._route_with_fallback(
            "meta/llama-3.1-8b-instruct", "/chat/completions", body
        )
        assert status == 200
        assert provider == "nvidia"
        assert real_model == "meta/llama-3.1-8b-instruct"
        assert fake.calls == [("nvidia", "meta/llama-3.1-8b-instruct")]

    def test_error_snippet_truncates_and_stays_secret_free(self):
        long_msg = "x" * 500
        snippet = opsora_server._error_snippet({"error": {"message": long_msg}})
        assert len(snippet) == 200
        # Non-dict / odd shapes never raise
        assert isinstance(opsora_server._error_snippet(None), str)
        assert isinstance(opsora_server._error_snippet({"weird": 1}), str)


class TestRouteWithFallbackStreaming:
    def test_primary_fails_fallback_succeeds_streaming(self, monkeypatch, caplog):
        sentinel_resp = object()
        outcomes = [
            (None, 429, {"error": {"message": "primary limited", "type": "rate_limit_error"}}),
            (sentinel_resp, 200, None),
        ]
        calls = []

        def fake_streaming(provider_name, endpoint, body, timeout=120):
            calls.append((provider_name, body.get("model")))
            return outcomes.pop(0)

        monkeypatch.setattr(opsora_server, "_call_provider_streaming", fake_streaming)

        body = {"model": "opsora-fast", "messages": [], "stream": True}
        with caplog.at_level(logging.WARNING, logger="opsora"):
            resp, status, provider, real_model, err = \
                opsora_server._route_with_fallback_streaming(
                    "opsora-fast", "/chat/completions", body
                )
        assert status == 200
        assert resp is sentinel_resp
        assert err is None
        assert provider == "nvidia"
        assert real_model == "stepfun-ai/step-3.7-flash"
        warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1
        assert "429" in warns[0] and "[stream]" in warns[0]

    def test_all_fail_streaming_returns_error_dict(self, monkeypatch):
        outcomes = [
            (None, 429, {"error": {"message": "a", "type": "rate_limit_error"}}),
            (None, 500, {"error": {"message": "b", "type": "upstream_error"}}),
            (None, 503, {"error": {"message": "c", "type": "upstream_error"}}),
        ]

        def fake_streaming(provider_name, endpoint, body, timeout=120):
            return outcomes.pop(0)

        monkeypatch.setattr(opsora_server, "_call_provider_streaming", fake_streaming)

        body = {"model": "opsora-fast", "messages": [], "stream": True}
        resp, status, provider, real_model, err = \
            opsora_server._route_with_fallback_streaming(
                "opsora-fast", "/chat/completions", body
            )
        assert resp is None
        assert status == 503
        assert err["error"]["message"] == "c"
        assert provider == "nvidia"
        assert real_model == "deepseek-ai/deepseek-v4-flash"
