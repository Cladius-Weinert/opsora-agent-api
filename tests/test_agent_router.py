"""Tests for agent_router: intent classification, model selection, routing."""

import agent_router
from agent_router import (
    INTENT_MODEL_MAP,
    detect_intent,
    route_agent_request,
    select_model,
)


def _user(text):
    return [{"role": "user", "content": text}]


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

class TestDetectIntent:
    def test_code_intent(self):
        msgs = _user("Write a Python function to debug this SQL query and handle the exception")
        assert detect_intent(msgs) == "code"

    def test_vision_intent(self):
        msgs = _user("Look at this image and describe the chart layout")
        assert detect_intent(msgs) == "vision"

    def test_reasoning_intent(self):
        msgs = _user("Analyze the probability distribution and calculate the mean step by step")
        assert detect_intent(msgs) == "reasoning"

    def test_creative_intent(self):
        msgs = _user("Write a catchy slogan and a poem for my blog post")
        assert detect_intent(msgs) == "creative"

    def test_general_when_no_keywords(self):
        msgs = _user("Hello, how are you today?")
        assert detect_intent(msgs) == "general"

    def test_single_keyword_below_threshold_is_general(self):
        # 'database' is one code keyword — below MIN_MATCH_THRESHOLD (2)
        msgs = _user("I have a question about my database")
        assert detect_intent(msgs) == "general"

    def test_mixed_intent_when_two_intents_strong(self):
        # creative: write, poem (2) + reasoning: analyze, statistics (2)
        msgs = _user("Write a poem and analyze the statistics of the data")
        assert detect_intent(msgs) == "mixed"

    def test_empty_messages_is_general(self):
        assert detect_intent([]) == "general"

    def test_no_user_message_is_general(self):
        msgs = [{"role": "system", "content": "You are helpful."}]
        assert detect_intent(msgs) == "general"

    def test_multimodal_content_list(self):
        msgs = [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                {"type": "text", "text": "analyze this image and the chart in it"},
            ],
        }]
        assert detect_intent(msgs) == "vision"

    def test_uses_last_user_message_only(self):
        msgs = [
            {"role": "user", "content": "Write a poem and a story"},
            {"role": "assistant", "content": "Sure!"},
            {"role": "user", "content": "Now debug this Python function and fix the exception"},
        ]
        assert detect_intent(msgs) == "code"


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

class TestSelectModel:
    def test_intent_model_map_aliases(self):
        assert INTENT_MODEL_MAP["code"] == "opsora-code"
        assert INTENT_MODEL_MAP["vision"] == "opsora-vision"
        assert INTENT_MODEL_MAP["reasoning"] == "opsora-reason"
        assert INTENT_MODEL_MAP["creative"] == "opsora-brain"
        assert INTENT_MODEL_MAP["general"] == "opsora-fast"

    def test_select_model_for_each_intent(self):
        assert select_model("code") == "opsora-code"
        assert select_model("vision") == "opsora-vision"
        assert select_model("reasoning") == "opsora-reason"
        assert select_model("creative") == "opsora-brain"
        assert select_model("general") == "opsora-fast"

    def test_select_model_unknown_defaults_to_brain(self):
        assert select_model("something-else") == "opsora-brain"


# ---------------------------------------------------------------------------
# route_agent_request with a mocked route_fn (no real API calls)
# ---------------------------------------------------------------------------

def _ok_route_fn(model_alias, endpoint, body):
    return (
        {
            "choices": [{"message": {"role": "assistant",
                                     "content": f"response from {model_alias}"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
        200,
        "nvidia",
        f"real-{model_alias}",
    )


def _fail_route_fn(model_alias, endpoint, body):
    return ({"error": {"message": "upstream down", "type": "upstream_error"}},
            502, "nvidia", model_alias)


class TestRouteAgentRequest:
    def test_single_intent_routes_to_mapped_model(self):
        body = {"model": "opsora-agent", "messages":
                _user("Debug this Python function and fix the exception")}
        result, status, provider, real_model = route_agent_request(
            body["messages"], body, _ok_route_fn
        )
        assert status == 200
        assert provider == "opsora-agent"
        meta = result["agent_metadata"]
        assert meta["intent"] == "code"
        assert meta["models_used"] == ["opsora-code"]
        assert result["choices"][0]["message"]["content"] == "response from opsora-code"

    def test_all_subtasks_failed_returns_502(self):
        body = {"model": "opsora-agent", "messages":
                _user("Debug this Python function and fix the exception")}
        result, status, provider, real_model = route_agent_request(
            body["messages"], body, _fail_route_fn
        )
        assert status == 502
        assert "errors" in result["choices"][0]["message"]["content"].lower()

    def test_decompose_non_mixed_returns_single_subtask(self):
        subtasks = agent_router.decompose_task(_user("hello"), "general")
        assert len(subtasks) == 1
        assert subtasks[0]["model"] == "opsora-fast"

    def test_synthesize_single_result_passthrough(self):
        results = [{"intent": "code", "model": "opsora-code", "content": "abc",
                    "provider": "nvidia", "real_model": "x", "status": 200}]
        assert agent_router._synthesize_results(results, []) == "abc"

    def test_synthesize_multiple_results_has_sections(self):
        results = [
            {"intent": "code", "model": "opsora-code", "content": "code part",
             "provider": "nvidia", "real_model": "x", "status": 200},
            {"intent": "reasoning", "model": "opsora-reason", "content": "analysis part",
             "provider": "nvidia", "real_model": "y", "status": 200},
        ]
        out = agent_router._synthesize_results(results, [])
        assert "code part" in out and "analysis part" in out
        # reasoning sorts before code
        assert out.index("analysis part") < out.index("code part")
