"""Tests for martol_agent.claude_code_wrapper — SSRF, deny-list, tool permissions."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock claude_agent_sdk before importing the wrapper
import sys

mock_sdk = MagicMock()
mock_sdk.ClaudeSDKClient = MagicMock()
mock_sdk.ClaudeAgentOptions = MagicMock()

mock_types = MagicMock()
mock_sdk.types = mock_types
mock_types.AssistantMessage = type("AssistantMessage", (), {})
mock_types.ResultMessage = type("ResultMessage", (), {})
mock_types.TextBlock = type("TextBlock", (), {})
mock_types.PermissionResultAllow = type("PermissionResultAllow", (), {"__init__": lambda self, **kw: None})
mock_types.PermissionResultDeny = type("PermissionResultDeny", (), {"__init__": lambda self, **kw: None})
mock_types.ToolPermissionContext = type("ToolPermissionContext", (), {})

sys.modules["claude_agent_sdk"] = mock_sdk
sys.modules["claude_agent_sdk.types"] = mock_types

from martol_agent.claude_code_wrapper import ClaudeCodeWrapper, DEFAULT_SAFE_TOOLS


# ── Helper ───────────────────────────────────────────────────────────


def _make_cc_wrapper(**kwargs):
    defaults = {
        "ws_url": "wss://example.com/api/rooms/1/ws",
        "api_key": "test-key",
        "context_size": 50,
        "respond_mode": "mention",
    }
    defaults.update(kwargs)
    return ClaudeCodeWrapper(**defaults)


# ── _tool_allowed ────────────────────────────────────────────────────


class TestToolAllowed:
    def test_exact_match(self):
        w = _make_cc_wrapper(claude_allowed_tools=["Read", "Write", "Bash"])
        assert w._tool_allowed("Read") is True
        assert w._tool_allowed("Write") is True

    def test_no_match(self):
        w = _make_cc_wrapper(claude_allowed_tools=["Read", "Write"])
        assert w._tool_allowed("Bash") is False

    def test_wildcard(self):
        w = _make_cc_wrapper(claude_allowed_tools=["mcp__playwright__*"])
        assert w._tool_allowed("mcp__playwright__click") is True
        assert w._tool_allowed("mcp__playwright__navigate") is True
        assert w._tool_allowed("mcp__other__click") is False

    def test_empty_list_uses_defaults(self):
        w = _make_cc_wrapper(claude_allowed_tools=[])
        # Should fall back to DEFAULT_SAFE_TOOLS
        assert w._tool_allowed("Read") is True
        assert w._tool_allowed("Grep") is True

    def test_default_safe_tools(self):
        w = _make_cc_wrapper()
        for tool in DEFAULT_SAFE_TOOLS:
            assert w._tool_allowed(tool) is True


# ── Path Deny-List ───────────────────────────────────────────────────


class TestPathDenyList:
    @pytest.mark.asyncio
    async def test_env_file_denied(self):
        w = _make_cc_wrapper(claude_allowed_tools=["Read", "Write"])
        context = MagicMock()
        result = await w._handle_permission("Read", {"file_path": "/project/.env"}, context)
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_env_variant_denied(self):
        w = _make_cc_wrapper(claude_allowed_tools=["Read"])
        context = MagicMock()
        result = await w._handle_permission("Read", {"file_path": "/project/.env.production"}, context)
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_key_file_denied(self):
        w = _make_cc_wrapper(claude_allowed_tools=["Read"])
        context = MagicMock()
        result = await w._handle_permission("Read", {"file_path": "/home/user/server.key"}, context)
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_pem_file_denied(self):
        w = _make_cc_wrapper(claude_allowed_tools=["Read"])
        context = MagicMock()
        result = await w._handle_permission("Read", {"file_path": "/certs/cert.pem"}, context)
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_normal_file_not_denied(self):
        w = _make_cc_wrapper(claude_allowed_tools=["Read"])
        context = MagicMock()
        # Mock MCP call for action_submit
        w._mcp_call = AsyncMock(return_value={"ok": True, "data": {"action_id": "a1"}})
        w._wait_for_approval = AsyncMock(return_value="approved")
        w.ws = AsyncMock()
        w.ws.send = AsyncMock()
        result = await w._handle_permission("Read", {"file_path": "/project/src/main.py"}, context)
        assert result.__class__.__name__ == "PermissionResultAllow"

    @pytest.mark.asyncio
    async def test_p12_file_denied(self):
        w = _make_cc_wrapper(claude_allowed_tools=["Read"])
        context = MagicMock()
        result = await w._handle_permission("Read", {"file_path": "/certs/keystore.p12"}, context)
        assert result.__class__.__name__ == "PermissionResultDeny"


# ── SSRF Protection ──────────────────────────────────────────────────


class TestSsrfProtection:
    @pytest.mark.asyncio
    async def test_private_10x_blocked(self):
        w = _make_cc_wrapper(claude_allowed_tools=["WebFetch"])
        context = MagicMock()
        result = await w._handle_permission("WebFetch", {"url": "http://10.0.0.1/admin"}, context)
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_private_172_blocked(self):
        w = _make_cc_wrapper(claude_allowed_tools=["WebFetch"])
        context = MagicMock()
        result = await w._handle_permission("WebFetch", {"url": "http://172.16.0.1/admin"}, context)
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_private_192_blocked(self):
        w = _make_cc_wrapper(claude_allowed_tools=["WebFetch"])
        context = MagicMock()
        result = await w._handle_permission("WebFetch", {"url": "http://192.168.1.1/admin"}, context)
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_loopback_blocked(self):
        w = _make_cc_wrapper(claude_allowed_tools=["WebFetch"])
        context = MagicMock()
        result = await w._handle_permission("WebFetch", {"url": "http://127.0.0.1:8080/secret"}, context)
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_link_local_blocked(self):
        w = _make_cc_wrapper(claude_allowed_tools=["WebFetch"])
        context = MagicMock()
        result = await w._handle_permission("WebFetch", {"url": "http://169.254.169.254/metadata"}, context)
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_public_ip_allowed(self):
        w = _make_cc_wrapper(claude_allowed_tools=["WebFetch"])
        context = MagicMock()
        # Mock action submission for approval flow
        w._mcp_call = AsyncMock(return_value={"ok": True, "data": {"action_id": "a1"}})
        w._wait_for_approval = AsyncMock(return_value="approved")
        w.ws = AsyncMock()
        w.ws.send = AsyncMock()
        result = await w._handle_permission("WebFetch", {"url": "https://api.example.com/data"}, context)
        # Domain name (not IP) passes SSRF check, proceeds to approval
        assert result.__class__.__name__ == "PermissionResultAllow"


# ── Default Safe Tools ───────────────────────────────────────────────


class TestDefaultSafeTools:
    def test_defaults_applied_when_empty(self):
        w = _make_cc_wrapper(claude_allowed_tools=[])
        assert w.claude_allowed_tools == DEFAULT_SAFE_TOOLS

    def test_custom_overrides_defaults(self):
        custom = ["Read", "Bash"]
        w = _make_cc_wrapper(claude_allowed_tools=custom)
        assert w.claude_allowed_tools == custom

    def test_default_list_contents(self):
        assert "Read" in DEFAULT_SAFE_TOOLS
        assert "Grep" in DEFAULT_SAFE_TOOLS
        assert "Glob" in DEFAULT_SAFE_TOOLS
        assert "Bash" not in DEFAULT_SAFE_TOOLS


# ── Response Chunking ────────────────────────────────────────────────


class TestResponseChunking:
    @pytest.mark.asyncio
    async def test_short_response_single_send(self):
        """Short messages should be sent in a single send_message call."""
        w = _make_cc_wrapper()
        w.ws = AsyncMock()
        w.ws.send = AsyncMock()
        await w.send_message("Short response", reply_to=1)
        w.ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_long_response_truncated_by_send(self):
        """Messages over 32KB are truncated by send_message."""
        w = _make_cc_wrapper()
        w.ws = AsyncMock()
        w.ws.send = AsyncMock()
        long_body = "x" * 40000
        await w.send_message(long_body)
        sent = json.loads(w.ws.send.call_args[0][0])
        assert "[truncated]" in sent["body"]
