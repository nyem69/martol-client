"""Tests for martol_agent.providers.anthropic — response parsing and formatting."""

import json
from unittest.mock import MagicMock, patch

import pytest

from martol_agent.providers import ToolCall, LLMResponse


@pytest.fixture
def provider():
    with patch("martol_agent.providers.anthropic.anthropic") as mock_anthropic, \
         patch("martol_agent.providers.anthropic.httpx", create=True) as mock_httpx:
        mock_httpx.Timeout = MagicMock()
        from martol_agent.providers.anthropic import AnthropicProvider
        return AnthropicProvider(api_key="test-key")


def _make_response(content_blocks, stop_reason="end_turn"):
    """Build a mock Anthropic Message."""
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    return resp


def _text_block(text):
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _tool_block(tool_id, name, input_data):
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = input_data
    return block


class TestParseResponse:
    def test_text_only(self, provider):
        resp = _make_response([_text_block("Hello world")])
        result = provider._parse_response(resp)
        assert result.text == "Hello world"
        assert result.tool_calls == []
        assert result.stop_reason == "end_turn"

    def test_tool_use(self, provider):
        resp = _make_response(
            [_tool_block("tc_1", "action_submit", {"action_type": "code_write"})],
            stop_reason="tool_use",
        )
        result = provider._parse_response(resp)
        assert result.text is None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "action_submit"
        assert result.tool_calls[0].arguments == {"action_type": "code_write"}
        assert result.stop_reason == "tool_use"

    def test_mixed_content(self, provider):
        resp = _make_response([
            _text_block("Let me check"),
            _tool_block("tc_2", "action_status", {"action_id": 42}),
        ], stop_reason="tool_use")
        result = provider._parse_response(resp)
        assert result.text == "Let me check"
        assert len(result.tool_calls) == 1

    def test_stop_reason_mapping(self, provider):
        for anthropic_reason, expected in [
            ("end_turn", "end_turn"),
            ("tool_use", "tool_use"),
            ("max_tokens", "max_tokens"),
            ("stop_sequence", "end_turn"),
            ("unknown_reason", "end_turn"),
        ]:
            resp = _make_response([_text_block("x")], stop_reason=anthropic_reason)
            result = provider._parse_response(resp)
            assert result.stop_reason == expected, f"Failed for {anthropic_reason}"

    def test_non_dict_input_defaults_to_empty(self, provider):
        resp = _make_response([_tool_block("tc_3", "test", "invalid")])
        result = provider._parse_response(resp)
        assert result.tool_calls[0].arguments == {}


class TestFormatToolResult:
    def test_format(self, provider):
        from martol_agent.providers.anthropic import AnthropicProvider
        result = AnthropicProvider.format_tool_result("tc_1", {"ok": True})
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "tc_1"
        assert json.loads(result["content"]) == {"ok": True}


class TestFormatAssistantMessage:
    def test_text_only(self, provider):
        from martol_agent.providers.anthropic import AnthropicProvider
        resp = LLMResponse(text="Hello")
        msg = AnthropicProvider.format_assistant_message(resp)
        assert msg["role"] == "assistant"
        assert len(msg["content"]) == 1
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][0]["text"] == "Hello"

    def test_with_tool_calls(self, provider):
        from martol_agent.providers.anthropic import AnthropicProvider
        tc = ToolCall(id="tc_1", name="action_submit", arguments={"x": 1})
        resp = LLMResponse(text="Checking", tool_calls=[tc])
        msg = AnthropicProvider.format_assistant_message(resp)
        assert msg["role"] == "assistant"
        assert len(msg["content"]) == 2
        assert msg["content"][1]["type"] == "tool_use"
        assert msg["content"][1]["id"] == "tc_1"
        assert msg["content"][1]["input"] == {"x": 1}
