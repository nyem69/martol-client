"""Tests for martol_agent.base_wrapper — URL derivation, HMAC, TLS, context, mention, MCP."""

import asyncio
import hashlib
import hmac as hmac_lib
import json
import os
import stat
import tempfile
from base64 import b64encode
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from martol_agent.base_wrapper import BaseWrapper, derive_mcp_url
from tests.conftest import sign_message


# ── derive_mcp_url ────────────────────────────────────────────────────


class TestDeriveMcpUrl:
    def test_wss_to_https(self):
        assert derive_mcp_url("wss://example.com/api/rooms/1/ws") == "https://example.com"

    def test_ws_to_http(self):
        assert derive_mcp_url("ws://localhost:3000/api/rooms/1/ws") == "http://localhost:3000"

    def test_with_port(self):
        assert derive_mcp_url("wss://example.com:8443/path") == "https://example.com:8443"

    def test_no_hostname_raises(self):
        with pytest.raises(ValueError, match="no hostname"):
            derive_mcp_url("wss:///no-host")

    def test_ipv6(self):
        result = derive_mcp_url("ws://[::1]:3000/path")
        assert result == "http://[::1]:3000"

    def test_plain_localhost(self):
        assert derive_mcp_url("ws://localhost/path") == "http://localhost"

    def test_preserves_only_scheme_and_host(self):
        result = derive_mcp_url("wss://example.com:443/api/rooms/123/ws?key=val")
        assert result == "https://example.com:443"


# ── Helper to create a BaseWrapper with overridable defaults ─────────


class ConcreteWrapper(BaseWrapper):
    """Concrete subclass for testing (BaseWrapper has abstract methods)."""

    async def _send_disclosure(self):
        pass

    async def _on_trigger(self, payload):
        self._last_trigger = payload


def _make_wrapper(
    ws_url="wss://example.com/api/rooms/1/ws",
    api_key="test-key",
    hmac_secret=None,
    allow_unsigned=False,
    context_size=50,
    respond_mode="mention",
):
    return ConcreteWrapper(
        ws_url=ws_url,
        api_key=api_key,
        hmac_secret=hmac_secret,
        allow_unsigned=allow_unsigned,
        context_size=context_size,
        respond_mode=respond_mode,
    )


# ── HMAC Verification ────────────────────────────────────────────────


class TestVerifyHmac:
    def test_no_secret_always_passes(self):
        w = _make_wrapper()
        assert w._verify_hmac('{"type":"message"}', {"type": "message"}) is True

    def test_valid_hmac(self):
        secret = "my-secret"
        w = _make_wrapper(hmac_secret=secret)
        msg = {"type": "message", "body": "hello"}
        raw = sign_message(msg, secret)
        parsed = json.loads(raw)
        assert w._verify_hmac(raw, parsed) is True

    def test_wrong_hmac(self):
        w = _make_wrapper(hmac_secret="correct-secret")
        msg = {"type": "message", "body": "hello"}
        raw = sign_message(msg, "wrong-secret")
        parsed = json.loads(raw)
        assert w._verify_hmac(raw, parsed) is False

    def test_missing_hmac_rejects(self):
        w = _make_wrapper(hmac_secret="my-secret")
        raw = '{"type":"message"}'
        parsed = json.loads(raw)
        assert w._verify_hmac(raw, parsed) is False

    def test_missing_hmac_allow_unsigned(self):
        w = _make_wrapper(hmac_secret="my-secret", allow_unsigned=True)
        raw = '{"type":"message"}'
        parsed = json.loads(raw)
        assert w._verify_hmac(raw, parsed) is True

    def test_invalid_base64_hmac(self):
        w = _make_wrapper(hmac_secret="my-secret")
        raw = '{"type":"message","_hmac":"not-valid-base64!!!"}'
        parsed = json.loads(raw)
        assert w._verify_hmac(raw, parsed) is False

    def test_hmac_suffix_not_matching(self):
        w = _make_wrapper(hmac_secret="my-secret")
        # _hmac not at the end of JSON
        raw = '{"_hmac":"dGVzdA==","type":"message"}'
        parsed = json.loads(raw)
        assert w._verify_hmac(raw, parsed) is False


# ── TLS Enforcement ──────────────────────────────────────────────────


class TestTlsEnforcement:
    @pytest.mark.asyncio
    async def test_rejects_ws_non_localhost(self):
        w = _make_wrapper(ws_url="ws://example.com/path")
        w._running = False  # prevent actual reconnect loop
        # connect() should raise ValueError for non-TLS non-local
        with patch("martol_agent.base_wrapper.websockets") as mock_ws:
            await w.connect()
            mock_ws.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepts_ws_localhost(self):
        w = _make_wrapper(ws_url="ws://localhost:3000/path")
        w._running = False
        with patch("martol_agent.base_wrapper.websockets"):
            w._http_session = AsyncMock()
            await w.connect()

    @pytest.mark.asyncio
    async def test_accepts_ws_127(self):
        w = _make_wrapper(ws_url="ws://127.0.0.1:3000/path")
        w._running = False
        with patch("martol_agent.base_wrapper.websockets"):
            w._http_session = AsyncMock()
            await w.connect()

    @pytest.mark.asyncio
    async def test_accepts_wss(self):
        w = _make_wrapper(ws_url="wss://example.com/path")
        w._running = False
        with patch("martol_agent.base_wrapper.websockets"):
            w._http_session = AsyncMock()
            await w.connect()


# ── _is_mentioned ────────────────────────────────────────────────────


class TestIsMentioned:
    def test_at_prefix(self):
        w = _make_wrapper()
        w.agent_name = "TestBot"
        assert w._is_mentioned("Hey @TestBot how are you?") is True

    def test_without_at(self):
        w = _make_wrapper()
        w.agent_name = "TestBot"
        assert w._is_mentioned("Hey TestBot how are you?") is True

    def test_case_insensitive(self):
        w = _make_wrapper()
        w.agent_name = "TestBot"
        assert w._is_mentioned("hey @testbot") is True

    def test_word_boundary(self):
        w = _make_wrapper()
        w.agent_name = "Bot"
        # "Robot" contains "Bot" but not as a word boundary
        assert w._is_mentioned("Hey Robot") is False

    def test_no_agent_name(self):
        w = _make_wrapper()
        w.agent_name = None
        assert w._is_mentioned("@anything") is False

    def test_special_chars_in_name(self):
        w = _make_wrapper()
        w.agent_name = "test.bot"
        assert w._is_mentioned("hey @test.bot") is True

    def test_not_mentioned(self):
        w = _make_wrapper()
        w.agent_name = "TestBot"
        assert w._is_mentioned("hello world") is False


# ── _should_respond ──────────────────────────────────────────────────


class TestShouldRespond:
    def test_all_mode_always_true(self):
        w = _make_wrapper(respond_mode="all")
        w.agent_name = "Bot"
        assert w._should_respond({"body": "hello"}) is True

    def test_mention_mode_with_mention(self):
        w = _make_wrapper(respond_mode="mention")
        w.agent_name = "Bot"
        assert w._should_respond({"body": "hey @Bot"}) is True

    def test_mention_mode_no_mention(self):
        w = _make_wrapper(respond_mode="mention")
        w.agent_name = "Bot"
        assert w._should_respond({"body": "hello world"}) is False

    def test_reply_to_self(self):
        w = _make_wrapper(respond_mode="mention")
        w.agent_name = "Bot"
        w.agent_user_id = "agent-123"
        # Add a message from the agent to conversation
        w.conversation.append({"id": 42, "sender_id": "agent-123", "body": "hi"})
        w._index_message("42", w.conversation[-1])
        assert w._should_respond({"body": "thanks", "replyTo": 42}) is True

    def test_reply_to_other(self):
        w = _make_wrapper(respond_mode="mention")
        w.agent_name = "Bot"
        w.agent_user_id = "agent-123"
        w.conversation.append({"id": 42, "sender_id": "user-456", "body": "hi"})
        w._index_message("42", w.conversation[-1])
        assert w._should_respond({"body": "thanks", "replyTo": 42}) is False

    def test_reply_to_unknown_id(self):
        w = _make_wrapper(respond_mode="mention")
        w.agent_name = "Bot"
        w.agent_user_id = "agent-123"
        assert w._should_respond({"body": "thanks", "replyTo": 999}) is False


# ── _append_context_from_ws ──────────────────────────────────────────


class TestAppendContextFromWs:
    def test_appends_message(self):
        w = _make_wrapper(context_size=5)
        w._append_context_from_ws({
            "serverSeqId": 1,
            "sender_id": "user-1",
            "sender_name": "Alice",
            "body": "hello",
        })
        assert len(w.conversation) == 1
        assert w.conversation[0]["body"] == "hello"

    def test_truncation(self):
        w = _make_wrapper(context_size=3)
        for i in range(5):
            w._append_context_from_ws({
                "serverSeqId": i + 1,
                "sender_id": "user-1",
                "sender_name": "Alice",
                "body": f"msg-{i}",
            })
        assert len(w.conversation) == 3
        assert w.conversation[0]["body"] == "msg-2"

    def test_dedup(self):
        w = _make_wrapper()
        payload = {"serverSeqId": 42, "sender_id": "u1", "sender_name": "A", "body": "hi"}
        w._append_context_from_ws(payload)
        w._append_context_from_ws(payload)
        assert len(w.conversation) == 1

    def test_updates_last_known_id(self):
        w = _make_wrapper()
        w._append_context_from_ws({"serverSeqId": 10, "sender_id": "u", "body": "a"})
        assert w.last_known_id == 10
        w._append_context_from_ws({"serverSeqId": 5, "sender_id": "u", "body": "b"})
        assert w.last_known_id == 10  # doesn't decrease

    def test_index_pruning(self):
        w = _make_wrapper()
        w._MESSAGE_INDEX_MAX = 3
        for i in range(5):
            w._append_context_from_ws({
                "serverSeqId": i + 1,
                "sender_id": "u",
                "sender_name": "A",
                "body": f"m{i}",
            })
        assert len(w._message_index) <= 3

    def test_seen_seq_pruning(self):
        w = _make_wrapper()
        w._SEEN_SEQ_MAX = 4
        for i in range(6):
            w._append_context_from_ws({
                "serverSeqId": i + 1,
                "sender_id": "u",
                "body": f"m{i}",
            })
        assert len(w._seen_seq_ids) <= 4

    def test_zero_seq_id_not_deduped(self):
        w = _make_wrapper()
        w._append_context_from_ws({"serverSeqId": 0, "sender_id": "u", "body": "a"})
        w._append_context_from_ws({"serverSeqId": 0, "sender_id": "u", "body": "b"})
        assert len(w.conversation) == 2


# ── _handle_message ──────────────────────────────────────────────────


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_routes_message_type(self):
        w = _make_wrapper(respond_mode="all")
        w.agent_user_id = "agent-1"
        w.agent_name = "Bot"
        msg = {
            "type": "message",
            "message": {
                "serverSeqId": 1,
                "sender_id": "user-1",
                "sender_name": "Alice",
                "body": "hello",
            },
        }
        await w._handle_message(msg)
        assert len(w.conversation) == 1
        assert hasattr(w, "_last_trigger")

    @pytest.mark.asyncio
    async def test_skips_own_message(self):
        w = _make_wrapper(respond_mode="all")
        w.agent_user_id = "agent-1"
        w.agent_name = "Bot"
        msg = {
            "type": "message",
            "message": {
                "serverSeqId": 2,
                "sender_id": "agent-1",
                "sender_name": "Bot",
                "body": "my own message",
            },
        }
        await w._handle_message(msg)
        assert not hasattr(w, "_last_trigger")

    @pytest.mark.asyncio
    async def test_hmac_failure_drops_message(self):
        w = _make_wrapper(hmac_secret="secret")
        w.agent_user_id = "agent-1"
        w.agent_name = "Bot"
        msg = {
            "type": "message",
            "message": {
                "serverSeqId": 3,
                "sender_id": "user-1",
                "body": "hello",
            },
        }
        await w._handle_message(msg)
        # No HMAC means message dropped — no context appended
        assert len(w.conversation) == 0

    @pytest.mark.asyncio
    async def test_history_type(self):
        w = _make_wrapper()
        w.agent_user_id = "agent-1"
        msg = {
            "type": "history",
            "messages": [
                {"serverSeqId": 1, "sender_id": "u1", "body": "a"},
                {"serverSeqId": 2, "sender_id": "u2", "body": "b"},
            ],
        }
        await w._handle_message(msg)
        assert len(w.conversation) == 2

    @pytest.mark.asyncio
    async def test_id_map_type(self):
        w = _make_wrapper()
        msg = {"type": "id_map", "localId": "abc", "serverSeqId": 10, "dbId": "db-10"}
        await w._handle_message(msg)
        assert w._id_map["10"] == "db-10"

    @pytest.mark.asyncio
    async def test_error_type_logged(self):
        w = _make_wrapper()
        msg = {"type": "error", "code": "AUTH_FAIL", "message": "bad key"}
        await w._handle_message(msg)  # Should not raise


# ── send_message ─────────────────────────────────────────────────────


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_success(self, mock_ws):
        w = _make_wrapper()
        w.ws = mock_ws
        result = await w.send_message("hello")
        assert result is True
        mock_ws.send.assert_called_once()
        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["body"] == "hello"
        assert sent["type"] == "message"

    @pytest.mark.asyncio
    async def test_no_ws_returns_false(self):
        w = _make_wrapper()
        w.ws = None
        result = await w.send_message("hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_truncation(self, mock_ws):
        w = _make_wrapper()
        w.ws = mock_ws
        long_body = "x" * 40000
        await w.send_message(long_body)
        sent = json.loads(mock_ws.send.call_args[0][0])
        assert "[truncated]" in sent["body"]
        assert len(sent["body"]) < 40000

    @pytest.mark.asyncio
    async def test_reply_to(self, mock_ws):
        w = _make_wrapper()
        w.ws = mock_ws
        await w.send_message("reply", reply_to=42)
        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["replyTo"] == 42


# ── warn_env_permissions ─────────────────────────────────────────────


class TestWarnEnvPermissions:
    def test_world_readable_warns(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("SECRET=value")
        env_file.chmod(0o644)
        # Should not raise (just logs warning)
        BaseWrapper.warn_env_permissions(str(env_file))

    def test_600_no_warning(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("SECRET=value")
        env_file.chmod(0o600)
        BaseWrapper.warn_env_permissions(str(env_file))

    def test_not_found_no_error(self):
        BaseWrapper.warn_env_permissions("/nonexistent/.env")


# ── _mcp_call ────────────────────────────────────────────────────────


def _mock_http_session(status=200, json_data=None, side_effect=None):
    """Create a properly mocked aiohttp session with context manager support."""
    session = MagicMock()
    if side_effect:
        session.post.side_effect = side_effect
        return session

    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data or {"ok": True})

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session.post.return_value = cm
    return session


class TestMcpCall:
    @pytest.mark.asyncio
    async def test_success(self):
        w = _make_wrapper()
        w._http_session = _mock_http_session(200, {"ok": True, "data": {}})
        result = await w._mcp_call("chat_who", {})
        assert result == {"ok": True, "data": {}}

    @pytest.mark.asyncio
    async def test_server_error_returns_none(self):
        w = _make_wrapper()
        w._http_session = _mock_http_session(500)
        result = await w._mcp_call("chat_who", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_redirect_blocked(self):
        w = _make_wrapper()
        w._http_session = _mock_http_session(302)
        result = await w._mcp_call("chat_who", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self):
        w = _make_wrapper()
        w._http_session = _mock_http_session(side_effect=Exception("Connection refused"))
        result = await w._mcp_call("chat_who", {})
        assert result is None
