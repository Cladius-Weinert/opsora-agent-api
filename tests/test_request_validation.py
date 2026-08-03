"""Tests for opsora_server._validate_chat_request edge cases."""

import opsora_server


def _validate(body):
    return opsora_server._validate_chat_request(body)


class TestValidateChatRequest:
    def test_missing_messages(self):
        ok, err = _validate({"model": "opsora-brain"})
        assert ok is False
        assert "'messages' field is required" in err

    def test_empty_messages_array(self):
        ok, err = _validate({"messages": []})
        assert ok is False
        assert "non-empty" in err

    def test_messages_not_a_list(self):
        ok, err = _validate({"messages": "hello"})
        assert ok is False
        assert "non-empty array" in err

    def test_message_not_an_object(self):
        ok, err = _validate({"messages": ["just a string"]})
        assert ok is False
        assert "messages[0] must be an object" in err

    def test_message_missing_role(self):
        ok, err = _validate({"messages": [{"content": "hi"}]})
        assert ok is False
        assert "missing 'role'" in err

    def test_user_message_missing_content(self):
        ok, err = _validate({"messages": [{"role": "user"}]})
        assert ok is False
        assert "missing 'content'" in err

    def test_assistant_message_without_content_is_valid(self):
        # e.g. placeholder before a tool call response
        ok, err = _validate({"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant"},
        ]})
        assert ok is True
        assert err is None

    def test_assistant_tool_call_without_content_is_valid(self):
        ok, err = _validate({"messages": [
            {"role": "user", "content": "list files"},
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "list_directory", "arguments": "{}"}}
            ]},
        ]})
        assert ok is True

    def test_valid_simple_request(self):
        ok, err = _validate({
            "model": "opsora-brain",
            "messages": [{"role": "user", "content": "Hello!"}],
        })
        assert ok is True
        assert err is None

    def test_error_index_points_at_bad_message(self):
        ok, err = _validate({"messages": [
            {"role": "user", "content": "ok"},
            {"role": "user"},  # index 1 is broken
        ]})
        assert ok is False
        assert "messages[1]" in err
