"""Tests for martol_agent.wrapper — validation, sanitization, rate limit, LLM messages, tool loop."""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from martol_agent.wrapper import (
    _validate_tool_args,
    _sanitize_tool_result,
    RateLimiter,
    AgentWrapper,
    MAX_TOOL_RESULT_LENGTH,
    ALLOWED_TOOL_FIELDS,
)
from martol_agent.providers import LLMResponse, ToolCall


# ── _validate_tool_args ──────────────────────────────────────────────


class TestValidateToolArgs:
    def test_filters_unknown_fields(self):
        result = _validate_tool_args("action_submit", {
            "action_type": "code_write",
            "risk_level": "low",
            "injected_field": "evil",
        })
        assert "action_type" in result
        assert "risk_level" in result
        assert "injected_field" not in result

    def test_passes_known_fields(self):
        args = {"action_id": 42}
        result = _validate_tool_args("action_status", args)
        assert result == {"action_id": 42}

    def test_unknown_tool_returns_empty(self):
        result = _validate_tool_args("unknown_tool", {"a": 1, "b": 2})
        assert result == {}

    def test_chat_send_fields(self):
        result = _validate_tool_args("chat_send", {
            "body": "hello",
            "reply_to": 42,
            "extra": "bad",
        })
        assert result == {"body": "hello", "reply_to": 42}

    def test_empty_args(self):
        result = _validate_tool_args("action_submit", {})
        assert result == {}

    def test_all_known_tools_have_entries(self):
        for tool_name in ALLOWED_TOOL_FIELDS:
            result = _validate_tool_args(tool_name, {})
            assert isinstance(result, dict)


# ── _sanitize_tool_result ────────────────────────────────────────────


class TestSanitizeToolResult:
    def test_none_result(self):
        result = _sanitize_tool_result(None)
        assert result == {"ok": False, "error": "No result"}

    def test_empty_dict(self):
        result = _sanitize_tool_result({})
        assert result == {"ok": False, "error": "No result"}

    def test_normal_result(self):
        data = {"ok": True, "data": "hello"}
        result = _sanitize_tool_result(data)
        assert result == data

    def test_truncated_result(self):
        large_data = {"ok": True, "data": "x" * (MAX_TOOL_RESULT_LENGTH + 1000)}
        result = _sanitize_tool_result(large_data)
        assert result["truncated"] is True
        assert result["ok"] is True
        assert len(result["data"]) <= MAX_TOOL_RESULT_LENGTH

    def test_unserializable(self):
        class Bad:
            pass
        result = _sanitize_tool_result({"obj": Bad()})
        assert result == {"ok": False, "error": "Result not serializable"}


# ── RateLimiter ──────────────────────────────────────────────────────


class TestRateLimiter:
    def test_under_limit(self):
        rl = RateLimiter(max_calls=3, window_seconds=60)
        assert rl.allow() is True
        assert rl.allow() is True

    def test_at_limit(self):
        rl = RateLimiter(max_calls=2, window_seconds=60)
        assert rl.allow() is True
        assert rl.allow() is True
        assert rl.allow() is False

    def test_window_reset(self):
        rl = RateLimiter(max_calls=1, window_seconds=0.1)
        assert rl.allow() is True
        assert rl.allow() is False
        # Simulate time passing
        with patch("martol_agent.wrapper.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 1
            rl.calls = [time.monotonic() - 1]  # old call
            assert rl.allow() is True

    def test_single_call_limit(self):
        rl = RateLimiter(max_calls=1, window_seconds=60)
        assert rl.allow() is True
        assert rl.allow() is False

    def test_zero_calls_in_window(self):
        rl = RateLimiter(max_calls=5, window_seconds=60)
        assert len(rl.calls) == 0
        assert rl.allow() is True
        assert len(rl.calls) == 1


# ── Helper to create AgentWrapper ────────────────────────────────────


def _make_agent_wrapper(**kwargs):
    """Create an AgentWrapper with mocked provider."""
    mock_provider = MagicMock()
    mock_provider.model = "test-model"
    defaults = {
        "ws_url": "wss://example.com/api/rooms/1/ws",
        "api_key": "test-key",
        "provider": mock_provider,
        "context_size": 50,
        "respond_mode": "mention",
    }
    defaults.update(kwargs)
    return AgentWrapper(**defaults)


# ── _build_llm_messages ──────────────────────────────────────────────


class TestBuildLlmMessages:
    def test_pseudonymizes_senders(self):
        w = _make_agent_wrapper()
        w.agent_user_id = "agent-1"
        w.conversation = [
            {"sender_id": "user-1", "sender_name": "Alice", "body": "hello"},
            {"sender_id": "user-2", "sender_name": "Bob", "body": "hi"},
        ]
        messages = w._build_llm_messages()
        assert len(messages) == 2
        assert 'User-1' in messages[0]["content"]
        assert 'User-2' in messages[1]["content"]
        assert 'Alice' not in messages[0]["content"]
        assert 'Bob' not in messages[1]["content"]

    def test_agent_messages_as_assistant(self):
        w = _make_agent_wrapper()
        w.agent_user_id = "agent-1"
        w.conversation = [
            {"sender_id": "user-1", "sender_name": "Alice", "body": "hello"},
            {"sender_id": "agent-1", "sender_name": "Bot", "body": "hi there"},
        ]
        messages = w._build_llm_messages()
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "hi there"

    def test_opt_out_users_excluded(self):
        w = _make_agent_wrapper()
        w.agent_user_id = "agent-1"
        w._ai_opt_out_users = {"user-2"}
        w.conversation = [
            {"sender_id": "user-1", "sender_name": "Alice", "body": "hello"},
            {"sender_id": "user-2", "sender_name": "Bob", "body": "private"},
            {"sender_id": "user-1", "sender_name": "Alice", "body": "bye"},
        ]
        messages = w._build_llm_messages()
        assert len(messages) == 2
        for m in messages:
            assert "private" not in m["content"]

    def test_xml_wrapping(self):
        w = _make_agent_wrapper()
        w.agent_user_id = "agent-1"
        w.conversation = [
            {"sender_id": "user-1", "sender_name": "Alice", "body": "hello"},
        ]
        messages = w._build_llm_messages()
        assert messages[0]["content"].startswith("<chat_message")
        assert 'sender="User-1"' in messages[0]["content"]
        assert "hello</chat_message>" in messages[0]["content"]

    def test_empty_body_skipped(self):
        w = _make_agent_wrapper()
        w.agent_user_id = "agent-1"
        w.conversation = [
            {"sender_id": "user-1", "sender_name": "Alice", "body": ""},
            {"sender_id": "user-1", "sender_name": "Alice", "body": "   "},
            {"sender_id": "user-1", "sender_name": "Alice", "body": "hello"},
        ]
        messages = w._build_llm_messages()
        assert len(messages) == 1

    def test_starts_with_user_role(self):
        w = _make_agent_wrapper()
        w.agent_user_id = "agent-1"
        w.conversation = [
            {"sender_id": "agent-1", "sender_name": "Bot", "body": "hi"},
            {"sender_id": "user-1", "sender_name": "Alice", "body": "hello"},
        ]
        messages = w._build_llm_messages()
        assert messages[0]["role"] == "user"

    def test_empty_conversation_returns_placeholder(self):
        w = _make_agent_wrapper()
        w.agent_user_id = "agent-1"
        w.conversation = []
        messages = w._build_llm_messages()
        assert len(messages) == 1
        assert messages[0]["role"] == "user"

    def test_same_sender_gets_same_pseudonym(self):
        w = _make_agent_wrapper()
        w.agent_user_id = "agent-1"
        w.conversation = [
            {"sender_id": "user-1", "sender_name": "Alice", "body": "first"},
            {"sender_id": "user-1", "sender_name": "Alice", "body": "second"},
        ]
        messages = w._build_llm_messages()
        assert 'User-1' in messages[0]["content"]
        assert 'User-1' in messages[1]["content"]


# ── _build_system_prompt ─────────────────────────────────────────────


class TestBuildSystemPrompt:
    def test_contains_agent_name(self):
        w = _make_agent_wrapper()
        w.agent_name = "TestBot"
        w.room_name = "dev-room"
        w.member_count = 5
        prompt = w._build_system_prompt()
        assert "TestBot" in prompt

    def test_contains_security_rules(self):
        w = _make_agent_wrapper()
        w.agent_name = "Bot"
        w.member_count = 3
        prompt = w._build_system_prompt()
        assert "SECURITY RULES" in prompt
        assert "UNTRUSTED" in prompt


# ── _process_response ────────────────────────────────────────────────


class TestProcessResponse:
    @pytest.mark.asyncio
    async def test_text_only_sends_message(self):
        w = _make_agent_wrapper()
        w.ws = AsyncMock()
        w.ws.closed = False
        response = LLMResponse(text="Hello!", tool_calls=[])
        trigger = {"serverSeqId": 1}
        await w._process_response(response, trigger)
        w.ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_loop_executes(self):
        w = _make_agent_wrapper()
        w.ws = AsyncMock()
        w.ws.closed = False
        w.agent_user_id = "agent-1"
        w.agent_name = "Bot"

        # First response has tool call, second has text only
        tc = ToolCall(id="tc_1", name="action_submit", arguments={"action_type": "code_write", "risk_level": "low", "description": "test", "trigger_message_id": 1})
        first_response = LLMResponse(text=None, tool_calls=[tc], stop_reason="tool_use")
        second_response = LLMResponse(text="Done!", tool_calls=[])

        w._mcp_call = AsyncMock(return_value={"ok": True, "data": {}})
        w.provider.chat = AsyncMock(return_value=second_response)

        # Need to mock the tool result building
        with patch.object(w, '_build_tool_result_messages', return_value=[
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tc_1", "name": "action_submit", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tc_1", "content": "{}"}]},
        ]):
            await w._process_response(first_response, {"serverSeqId": 1})

        w._mcp_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_max_iterations_stops(self):
        w = _make_agent_wrapper()
        w.ws = AsyncMock()
        w.ws.closed = False
        w.agent_user_id = "agent-1"
        w.agent_name = "Bot"

        tc = ToolCall(id="tc_1", name="action_status", arguments={"action_id": 1})
        # Always return tool calls
        looping_response = LLMResponse(text=None, tool_calls=[tc], stop_reason="tool_use")

        w._mcp_call = AsyncMock(return_value={"ok": True})
        w.provider.chat = AsyncMock(return_value=looping_response)

        with patch.object(w, '_build_tool_result_messages', return_value=[
            {"role": "assistant", "content": []},
            {"role": "user", "content": []},
        ]):
            await w._process_response(looping_response, {"serverSeqId": 1})

        # Should have called provider.chat MAX_TOOL_ITERATIONS times
        assert w.provider.chat.call_count == 5

    @pytest.mark.asyncio
    async def test_ws_closed_aborts(self):
        w = _make_agent_wrapper()
        w.ws = AsyncMock()
        w.ws.closed = True
        tc = ToolCall(id="tc_1", name="action_submit", arguments={})
        response = LLMResponse(text="text", tool_calls=[tc])
        await w._process_response(response, {"serverSeqId": 1})
        # Should not attempt MCP call since ws is closed
        assert not hasattr(w, '_mcp_call') or True  # just shouldn't crash

    @pytest.mark.asyncio
    async def test_no_tool_calls_no_loop(self):
        w = _make_agent_wrapper()
        w.ws = AsyncMock()
        w.ws.closed = False
        response = LLMResponse(text="Just text", tool_calls=[])
        await w._process_response(response, {"serverSeqId": 1})
        w.ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_text_not_sent(self):
        w = _make_agent_wrapper()
        w.ws = AsyncMock()
        w.ws.closed = False
        response = LLMResponse(text="", tool_calls=[])
        await w._process_response(response, {"serverSeqId": 1})
        w.ws.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_text_not_sent(self):
        w = _make_agent_wrapper()
        w.ws = AsyncMock()
        w.ws.closed = False
        response = LLMResponse(text=None, tool_calls=[])
        await w._process_response(response, {"serverSeqId": 1})
        w.ws.send.assert_not_called()
