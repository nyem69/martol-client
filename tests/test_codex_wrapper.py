"""Tests for martol_agent.codex_wrapper — init, RPC, tool calls, response parsing."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from martol_agent.codex_wrapper import CodexWrapper


# ── Helper ───────────────────────────────────────────────────────────


def _make_wrapper(**kwargs):
    defaults = {
        "ws_url": "wss://example.com/api/rooms/1/ws",
        "api_key": "test-key",
        "context_size": 50,
        "respond_mode": "mention",
    }
    defaults.update(kwargs)
    return CodexWrapper(**defaults)


def _mock_proc(stdout_lines: list[bytes] | None = None):
    """Create a mock asyncio subprocess with controllable stdin/stdout/stderr."""
    proc = MagicMock()
    proc.pid = 12345
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()
    proc.wait = AsyncMock()

    # Queue of lines for stdout.readline()
    lines = list(stdout_lines or [])
    async def readline():
        if lines:
            return lines.pop(0)
        return b""
    proc.stdout = MagicMock()
    proc.stdout.readline = readline

    proc.stderr = MagicMock()
    proc.stderr.readline = AsyncMock(return_value=b"")

    return proc


# ── Constructor ──────────────────────────────────────────────────────


class TestCodexWrapperInit:
    def test_defaults(self):
        w = _make_wrapper()
        assert w.codex_model is None
        assert w.codex_sandbox == "read-only"
        assert w.codex_approval_policy == "on-failure"
        assert w._thread_id is None
        assert w._codex_proc is None
        assert w._rpc_id == 0

    def test_custom_options(self):
        w = _make_wrapper(
            codex_model="o3",
            codex_sandbox="workspace-write",
            codex_approval_policy="never",
        )
        assert w.codex_model == "o3"
        assert w.codex_sandbox == "workspace-write"
        assert w.codex_approval_policy == "never"


# ── Disclosure ───────────────────────────────────────────────────────


class TestDisclosure:
    @pytest.mark.asyncio
    async def test_disclosure_includes_codex(self):
        w = _make_wrapper()
        w.agent_name = "test-bot"
        w.ws = AsyncMock()
        w.ws.send = AsyncMock()
        await w._send_disclosure()
        sent = json.loads(w.ws.send.call_args[0][0])
        assert "OpenAI Codex" in sent["body"]
        assert "test-bot" in sent["body"]

    @pytest.mark.asyncio
    async def test_disclosure_shows_model(self):
        w = _make_wrapper(codex_model="o4-mini")
        w.agent_name = "bot"
        w.ws = AsyncMock()
        w.ws.send = AsyncMock()
        await w._send_disclosure()
        sent = json.loads(w.ws.send.call_args[0][0])
        assert "o4-mini" in sent["body"]

    @pytest.mark.asyncio
    async def test_disclosure_default_model(self):
        w = _make_wrapper()
        w.agent_name = "bot"
        w.ws = AsyncMock()
        w.ws.send = AsyncMock()
        await w._send_disclosure()
        sent = json.loads(w.ws.send.call_args[0][0])
        assert "default" in sent["body"]


# ── RPC Call ─────────────────────────────────────────────────────────


class TestRpcCall:
    @pytest.mark.asyncio
    async def test_rpc_call_sends_json_rpc(self):
        w = _make_wrapper()
        response = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        proc = _mock_proc([json.dumps(response).encode() + b"\n"])
        w._codex_proc = proc

        result = await w._rpc_call("test/method", {"key": "value"})

        assert result == {"ok": True}
        written = proc.stdin.write.call_args[0][0]
        request = json.loads(written.decode())
        assert request["jsonrpc"] == "2.0"
        assert request["id"] == 1
        assert request["method"] == "test/method"
        assert request["params"] == {"key": "value"}

    @pytest.mark.asyncio
    async def test_rpc_call_increments_id(self):
        w = _make_wrapper()
        # First call
        resp1 = {"jsonrpc": "2.0", "id": 1, "result": {"a": 1}}
        proc1 = _mock_proc([json.dumps(resp1).encode() + b"\n"])
        w._codex_proc = proc1
        await w._rpc_call("m1", {})
        assert w._rpc_id == 1

        # Second call
        resp2 = {"jsonrpc": "2.0", "id": 2, "result": {"b": 2}}
        proc2 = _mock_proc([json.dumps(resp2).encode() + b"\n"])
        w._codex_proc = proc2
        await w._rpc_call("m2", {})
        assert w._rpc_id == 2

    @pytest.mark.asyncio
    async def test_rpc_call_returns_none_on_error(self):
        w = _make_wrapper()
        response = {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "fail"}}
        proc = _mock_proc([json.dumps(response).encode() + b"\n"])
        w._codex_proc = proc

        result = await w._rpc_call("bad/method", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_rpc_call_returns_none_when_no_process(self):
        w = _make_wrapper()
        w._codex_proc = None
        result = await w._rpc_call("test", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_rpc_call_skips_notifications(self):
        """Notifications (no id) should be skipped, response still received."""
        w = _make_wrapper()
        notification = {"jsonrpc": "2.0", "method": "some/notification", "params": {}}
        response = {"jsonrpc": "2.0", "id": 1, "result": {"data": "ok"}}
        proc = _mock_proc([
            json.dumps(notification).encode() + b"\n",
            json.dumps(response).encode() + b"\n",
        ])
        w._codex_proc = proc

        result = await w._rpc_call("test", {})
        assert result == {"data": "ok"}

    @pytest.mark.asyncio
    async def test_rpc_call_skips_non_json(self):
        """Non-JSON lines should be skipped."""
        w = _make_wrapper()
        response = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        proc = _mock_proc([
            b"some debug output\n",
            json.dumps(response).encode() + b"\n",
        ])
        w._codex_proc = proc

        result = await w._rpc_call("test", {})
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_rpc_call_timeout(self):
        """Should return None on timeout, not raise."""
        w = _make_wrapper()
        proc = MagicMock()
        proc.pid = 1
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdout = MagicMock()
        # Simulate hanging — readline never returns
        proc.stdout.readline = AsyncMock(side_effect=asyncio.TimeoutError)
        w._codex_proc = proc

        result = await w._rpc_call("test", {}, timeout=0.1)
        assert result is None
        assert 1 not in w._pending_requests  # cleaned up


# ── RPC Notify ───────────────────────────────────────────────────────


class TestRpcNotify:
    @pytest.mark.asyncio
    async def test_notify_sends_without_id(self):
        w = _make_wrapper()
        proc = _mock_proc()
        w._codex_proc = proc

        await w._rpc_notify("notifications/initialized", {})

        written = proc.stdin.write.call_args[0][0]
        msg = json.loads(written.decode())
        assert "id" not in msg
        assert msg["method"] == "notifications/initialized"

    @pytest.mark.asyncio
    async def test_notify_noop_without_process(self):
        w = _make_wrapper()
        w._codex_proc = None
        await w._rpc_notify("test", {})  # should not raise


# ── Tool Calls ───────────────────────────────────────────────────────


class TestCallCodexTool:
    @pytest.mark.asyncio
    async def test_first_call_uses_codex_tool(self):
        """First call should use 'codex' tool with full config."""
        w = _make_wrapper(codex_model="o3", codex_sandbox="workspace-write")
        w.agent_name = "bot"
        w.room_name = "test-room"

        async def mock_rpc(method, params, timeout=120):
            if method == "tools/call":
                assert params["name"] == "codex"
                args = params["arguments"]
                assert args["prompt"] == "hello"
                assert args["model"] == "o3"
                assert args["sandbox"] == "workspace-write"
                assert "developer-instructions" in args
                return {
                    "content": [{"type": "text", "text": json.dumps({
                        "threadId": "t-123",
                        "content": "Hello back!"
                    })}]
                }
            return None

        w._rpc_call = mock_rpc
        result = await w._call_codex_tool("hello")
        assert result == "Hello back!"
        assert w._thread_id == "t-123"

    @pytest.mark.asyncio
    async def test_subsequent_call_uses_reply_tool(self):
        """After thread is established, should use 'codex-reply'."""
        w = _make_wrapper()
        w._thread_id = "t-existing"

        async def mock_rpc(method, params, timeout=120):
            if method == "tools/call":
                assert params["name"] == "codex-reply"
                assert params["arguments"]["threadId"] == "t-existing"
                assert params["arguments"]["prompt"] == "follow up"
                return {
                    "content": [{"type": "text", "text": json.dumps({
                        "threadId": "t-existing",
                        "content": "Reply content"
                    })}]
                }
            return None

        w._rpc_call = mock_rpc
        result = await w._call_codex_tool("follow up")
        assert result == "Reply content"

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_result(self):
        w = _make_wrapper()

        async def mock_rpc(method, params, timeout=120):
            return None

        w._rpc_call = mock_rpc
        result = await w._call_codex_tool("hello")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_content(self):
        w = _make_wrapper()

        async def mock_rpc(method, params, timeout=120):
            return {"content": []}

        w._rpc_call = mock_rpc
        result = await w._call_codex_tool("hello")
        assert result is None

    @pytest.mark.asyncio
    async def test_plain_text_response(self):
        """Non-JSON text parts should be returned as-is."""
        w = _make_wrapper()

        async def mock_rpc(method, params, timeout=120):
            return {
                "content": [{"type": "text", "text": "plain response"}]
            }

        w._rpc_call = mock_rpc
        result = await w._call_codex_tool("hello")
        assert result == "plain response"
        assert w._thread_id is None  # no threadId in plain text

    @pytest.mark.asyncio
    async def test_multiple_text_parts_joined(self):
        w = _make_wrapper()

        async def mock_rpc(method, params, timeout=120):
            return {
                "content": [
                    {"type": "text", "text": "part one"},
                    {"type": "text", "text": "part two"},
                ]
            }

        w._rpc_call = mock_rpc
        result = await w._call_codex_tool("hello")
        assert result == "part one\npart two"

    @pytest.mark.asyncio
    async def test_first_call_without_model(self):
        """When no model specified, should not include model in arguments."""
        w = _make_wrapper(codex_model=None)
        w.agent_name = "bot"

        async def mock_rpc(method, params, timeout=120):
            if method == "tools/call":
                assert "model" not in params["arguments"]
                return {"content": [{"type": "text", "text": "ok"}]}
            return None

        w._rpc_call = mock_rpc
        await w._call_codex_tool("test")

    @pytest.mark.asyncio
    async def test_brief_injected_into_instructions(self):
        """Project brief should be included in developer instructions."""
        w = _make_wrapper()
        w.agent_name = "bot"
        w.room_brief = json.dumps({"goal": "Build a thing", "stack": "Python"})

        captured_args = {}

        async def mock_rpc(method, params, timeout=120):
            if method == "tools/call":
                captured_args.update(params["arguments"])
                return {"content": [{"type": "text", "text": "ok"}]}
            return None

        w._rpc_call = mock_rpc
        await w._call_codex_tool("test")

        instructions = captured_args["developer-instructions"]
        assert "Build a thing" in instructions
        assert "Python" in instructions

    @pytest.mark.asyncio
    async def test_raw_brief_injected(self):
        """Non-JSON brief should be injected as raw text."""
        w = _make_wrapper()
        w.agent_name = "bot"
        w.room_brief = "This is a plain text brief"

        captured_args = {}

        async def mock_rpc(method, params, timeout=120):
            if method == "tools/call":
                captured_args.update(params["arguments"])
                return {"content": [{"type": "text", "text": "ok"}]}
            return None

        w._rpc_call = mock_rpc
        await w._call_codex_tool("test")
        assert "plain text brief" in captured_args["developer-instructions"]


# ── Trigger Gating ───────────────────────────────────────────────────


class TestTriggerGating:
    @pytest.mark.asyncio
    async def test_skips_when_already_responding(self):
        """Should skip trigger when _responding lock is held."""
        w = _make_wrapper()
        # Acquire the lock to simulate an in-progress response
        await w._responding.acquire()
        try:
            # _on_trigger should return without creating a task
            await w._on_trigger({"body": "test", "senderName": "user"})
            # No crash, no task created — skipped
        finally:
            w._responding.release()


# ── Lifecycle ────────────────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_stop_clears_thread_id(self):
        w = _make_wrapper()
        w._thread_id = "t-123"
        proc = _mock_proc()
        w._codex_proc = proc
        w._reader_task = asyncio.create_task(asyncio.sleep(10))

        await w._stop_codex_server()

        assert w._thread_id is None
        assert w._codex_proc is None

    @pytest.mark.asyncio
    async def test_stop_noop_when_no_process(self):
        w = _make_wrapper()
        w._codex_proc = None
        await w._stop_codex_server()  # should not raise

    @pytest.mark.asyncio
    async def test_start_logs_error_without_codex_binary(self):
        w = _make_wrapper()
        with patch("shutil.which", return_value=None):
            await w._start_codex_server()
        assert w._codex_proc is None


# ── Send to Codex ────────────────────────────────────────────────────


class TestSendToCodex:
    @pytest.mark.asyncio
    async def test_sends_formatted_prompt(self):
        """Should format prompt as [sender]: body."""
        w = _make_wrapper()
        proc = _mock_proc()
        w._codex_proc = proc
        w.ws = AsyncMock()
        w.ws.send = AsyncMock()

        captured_prompt = None

        async def mock_call(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            return "response text"

        w._call_codex_tool = mock_call

        trigger = {"senderName": "alice", "body": "hello world", "serverSeqId": 42}
        await w._send_to_codex(trigger)

        assert captured_prompt == "[alice]: hello world"

    @pytest.mark.asyncio
    async def test_sends_typing_indicators(self):
        w = _make_wrapper()
        proc = _mock_proc()
        w._codex_proc = proc
        w.ws = AsyncMock()
        w.ws.send = AsyncMock()

        typing_calls = []
        original_send_typing = w.send_typing

        async def track_typing(is_typing):
            typing_calls.append(is_typing)

        w.send_typing = track_typing
        w._call_codex_tool = AsyncMock(return_value="response")

        await w._send_to_codex({"senderName": "bob", "body": "hi", "serverSeqId": 1})

        assert typing_calls[0] is True  # typing started
        assert typing_calls[-1] is False  # typing stopped

    @pytest.mark.asyncio
    async def test_handles_empty_response(self):
        """Empty response should not crash, just log warning."""
        w = _make_wrapper()
        proc = _mock_proc()
        w._codex_proc = proc
        w.ws = AsyncMock()
        w.ws.send = AsyncMock()
        w._call_codex_tool = AsyncMock(return_value=None)

        await w._send_to_codex({"senderName": "bob", "body": "hi", "serverSeqId": 1})
        # Should not crash, send_message not called
        # (typing start + typing stop via ws.send, but no message body)

    @pytest.mark.asyncio
    async def test_chunks_long_response(self):
        """Responses over 4000 chars should be chunked."""
        w = _make_wrapper()
        proc = _mock_proc()
        w._codex_proc = proc
        w.ws = AsyncMock()
        w.ws.send = AsyncMock()

        long_response = "x" * 8500  # should produce 3 chunks
        w._call_codex_tool = AsyncMock(return_value=long_response)

        await w._send_to_codex({"senderName": "bob", "body": "hi", "serverSeqId": 1})

        # Count message sends (exclude typing indicators)
        message_sends = [
            call for call in w.ws.send.call_args_list
            if '"type": "message"' in call[0][0]
        ]
        assert len(message_sends) == 3

    @pytest.mark.asyncio
    async def test_first_chunk_has_reply_to(self):
        """First chunk should include replyTo, subsequent chunks should not."""
        w = _make_wrapper()
        proc = _mock_proc()
        w._codex_proc = proc
        w.ws = AsyncMock()
        w.ws.send = AsyncMock()

        long_response = "x" * 5000  # 2 chunks
        w._call_codex_tool = AsyncMock(return_value=long_response)

        await w._send_to_codex({"senderName": "bob", "body": "hi", "serverSeqId": 99})

        message_sends = [
            json.loads(call[0][0])
            for call in w.ws.send.call_args_list
            if '"type": "message"' in call[0][0]
        ]
        assert message_sends[0].get("replyTo") == 99
        assert "replyTo" not in message_sends[1]
