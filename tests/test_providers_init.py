"""Tests for martol_agent.providers — dataclasses and factory."""

import pytest
from unittest.mock import patch, MagicMock

from martol_agent.providers import ToolCall, LLMResponse, create_provider


class TestToolCall:
    def test_construction(self):
        tc = ToolCall(id="tc_1", name="action_submit", arguments={"action_type": "code_write"})
        assert tc.id == "tc_1"
        assert tc.name == "action_submit"
        assert tc.arguments == {"action_type": "code_write"}

    def test_empty_arguments(self):
        tc = ToolCall(id="tc_2", name="action_status", arguments={})
        assert tc.arguments == {}


class TestLLMResponse:
    def test_text_only(self):
        resp = LLMResponse(text="Hello")
        assert resp.text == "Hello"
        assert resp.tool_calls == []
        assert resp.stop_reason == "end_turn"

    def test_with_tool_calls(self):
        tc = ToolCall(id="1", name="test", arguments={})
        resp = LLMResponse(text=None, tool_calls=[tc], stop_reason="tool_use")
        assert resp.text is None
        assert len(resp.tool_calls) == 1
        assert resp.stop_reason == "tool_use"

    def test_default_stop_reason(self):
        resp = LLMResponse(text="hi")
        assert resp.stop_reason == "end_turn"


class TestCreateProvider:
    @patch("martol_agent.providers.anthropic.anthropic")
    @patch("martol_agent.providers.anthropic.httpx", create=True)
    def test_creates_anthropic(self, mock_httpx, mock_anthropic):
        mock_httpx.Timeout = MagicMock()
        provider = create_provider("anthropic", "test-key")
        assert provider.__class__.__name__ == "AnthropicProvider"

    @patch("martol_agent.providers.openai_compat.openai")
    @patch("martol_agent.providers.openai_compat.httpx", create=True)
    def test_creates_openai(self, mock_httpx, mock_openai):
        mock_httpx.Timeout = MagicMock()
        provider = create_provider("openai", "test-key")
        assert provider.__class__.__name__ == "OpenAICompatProvider"

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            create_provider("gemini", "test-key")
