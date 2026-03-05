"""Tests for martol_agent.providers.openai_compat — response parsing and formatting."""

import json
from unittest.mock import MagicMock, patch

import pytest

from martol_agent.providers import ToolCall, LLMResponse


@pytest.fixture
def provider():
    with patch("martol_agent.providers.openai_compat.openai") as mock_openai, \
         patch("martol_agent.providers.openai_compat.httpx", create=True) as mock_httpx:
        mock_httpx.Timeout = MagicMock()
        from martol_agent.providers.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(api_key="test-key")


def _make_response(content=None, tool_calls=None, finish_reason="stop"):
    """Build a mock OpenAI ChatCompletion."""
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls

    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason

    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _tool_call(tc_id, name, arguments_json):
    tc = MagicMock()
    tc.id = tc_id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments_json
    return tc


class TestParseResponse:
    def test_text_only(self, provider):
        resp = _make_response(content="Hello world")
        result = provider._parse_response(resp)
        assert result.text == "Hello world"
        assert result.tool_calls == []
        assert result.stop_reason == "end_turn"

    def test_tool_calls(self, provider):
        tc = _tool_call("tc_1", "action_submit", '{"action_type": "code_write"}')
        resp = _make_response(tool_calls=[tc], finish_reason="tool_calls")
        result = provider._parse_response(resp)
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "action_submit"
        assert result.tool_calls[0].arguments == {"action_type": "code_write"}
        assert result.stop_reason == "tool_use"

    def test_invalid_json_arguments(self, provider):
        tc = _tool_call("tc_2", "test", "not-json")
        resp = _make_response(tool_calls=[tc], finish_reason="tool_calls")
        result = provider._parse_response(resp)
        assert result.tool_calls[0].arguments == {}

    def test_finish_reason_mapping(self, provider):
        for openai_reason, expected in [
            ("stop", "end_turn"),
            ("tool_calls", "tool_use"),
            ("length", "max_tokens"),
            ("content_filter", "end_turn"),
            (None, "end_turn"),
        ]:
            resp = _make_response(content="x", finish_reason=openai_reason)
            result = provider._parse_response(resp)
            assert result.stop_reason == expected, f"Failed for {openai_reason}"

    def test_none_content(self, provider):
        resp = _make_response(content=None)
        result = provider._parse_response(resp)
        assert result.text is None


class TestFormatToolResult:
    def test_format(self, provider):
        from martol_agent.providers.openai_compat import OpenAICompatProvider
        result = OpenAICompatProvider.format_tool_result("tc_1", {"ok": True, "data": "test"})
        assert result["role"] == "tool"
        assert result["tool_call_id"] == "tc_1"
        assert json.loads(result["content"]) == {"ok": True, "data": "test"}


class TestFormatAssistantMessage:
    def test_text_only(self, provider):
        from martol_agent.providers.openai_compat import OpenAICompatProvider
        resp = LLMResponse(text="Hello")
        msg = OpenAICompatProvider.format_assistant_message(resp)
        assert msg["role"] == "assistant"
        assert msg["content"] == "Hello"
        assert "tool_calls" not in msg

    def test_with_tool_calls(self, provider):
        from martol_agent.providers.openai_compat import OpenAICompatProvider
        tc = ToolCall(id="tc_1", name="action_submit", arguments={"x": 1})
        resp = LLMResponse(text=None, tool_calls=[tc])
        msg = OpenAICompatProvider.format_assistant_message(resp)
        assert msg["role"] == "assistant"
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["type"] == "function"
        assert msg["tool_calls"][0]["function"]["name"] == "action_submit"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"x": 1}
