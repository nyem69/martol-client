#!/usr/bin/env python3
"""
Claude Code Bridge — connects a Claude Code session to a Martol chat room.

Instead of calling an LLM API directly, this wrapper manages a persistent
Claude Code subprocess via the Agent SDK. Chat messages become prompts,
Claude Code responses become chat messages, and tool permissions are
relayed to the room for approval via action_submit.
"""

import asyncio
import json
import logging
import os
import time

try:
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    from claude_agent_sdk.types import (
        AssistantMessage,
        ResultMessage,
        TextBlock,
        PermissionResultAllow,
        PermissionResultDeny,
        ToolPermissionContext,
    )
except ImportError:
    print("Error: claude-agent-sdk required. pip install claude-agent-sdk")
    import sys
    sys.exit(1)

from martol_agent.base_wrapper import BaseWrapper

log = logging.getLogger("martol-agent")

# Default safe tools if no whitelist specified
DEFAULT_SAFE_TOOLS = ["Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch"]


class ClaudeCodeWrapper(BaseWrapper):
    """Bridges a Martol chat room to a persistent Claude Code session."""

    def __init__(
        self,
        ws_url: str,
        api_key: str,
        mcp_url: str | None = None,
        context_size: int = 50,
        respond_mode: str = "mention",
        claude_model: str | None = None,
        claude_permission_mode: str = "default",
        claude_allowed_tools: list[str] | None = None,
        hmac_secret: str | None = None,
        allow_unsigned: bool = False,
    ):
        super().__init__(
            ws_url=ws_url,
            api_key=api_key,
            mcp_url=mcp_url,
            context_size=context_size,
            respond_mode=respond_mode,
            hmac_secret=hmac_secret,
            allow_unsigned=allow_unsigned,
        )
        self.claude_model = claude_model
        self.claude_permission_mode = claude_permission_mode
        self.claude_allowed_tools = claude_allowed_tools or []
        self.member_count: int = 0

        # Apply safe defaults if no whitelist specified
        if not self.claude_allowed_tools:
            log.info("No tool whitelist set — using safe defaults: %s", DEFAULT_SAFE_TOOLS)
            self.claude_allowed_tools = list(DEFAULT_SAFE_TOOLS)

        # Claude Code SDK client (persistent session)
        self.claude_client: ClaudeSDKClient | None = None

    # ── Disclosure ────────────────────────────────────────────────────

    async def _send_disclosure(self):
        """Send AI disclosure message for Claude Code mode."""
        display = self.agent_name or "agent"
        model_display = self.claude_model or "default"
        await self.send_message(
            f"[AI Agent] {display} connected (powered by Claude Code, model: {model_display}). "
            f"I am an AI assistant with access to this project's codebase. "
            f"Responses should not be relied upon without verification."
        )

    # ── Trigger ───────────────────────────────────────────────────────

    async def _on_trigger(self, payload: dict):
        """Handle a message that should trigger a Claude Code response."""
        asyncio.create_task(self._send_to_claude(payload))

    # ── Claude Code Session ──────────────────────────────────────────

    async def _start_claude_session(self) -> None:
        """Start a persistent Claude Code SDK session."""
        display = self.agent_name or "agent"
        system_append = (
            f"\nYou are {display}, an AI coding assistant in a Martol chat room "
            f'called "{self.room_name or "unknown"}" with {self.member_count} members.\n'
            f"You respond when mentioned with @{display}.\n"
            f"Keep responses concise. When showing code or file contents, "
            f"use markdown formatting.\n"
        )

        options = ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code", "append": system_append},
            permission_mode=self.claude_permission_mode,
            allowed_tools=self.claude_allowed_tools,
            can_use_tool=self._handle_permission,
            cwd=os.getcwd(),
            include_partial_messages=False,
            setting_sources=["user", "project", "local"],
        )
        if self.claude_model:
            options.model = self.claude_model

        self.claude_client = ClaudeSDKClient(options=options)
        await self.claude_client.connect()
        log.info("Claude Code session started")

    async def _stop_claude_session(self) -> None:
        """Stop the Claude Code SDK session."""
        if self.claude_client:
            try:
                await self.claude_client.disconnect()
            except Exception:
                pass
            self.claude_client = None
            log.info("Claude Code session stopped")

    # ── Connection override ──────────────────────────────────────────

    async def connect(self) -> None:
        """Connect with Claude Code session management."""
        # Override to add Claude session start/stop around the base connect
        self._http_session = None
        import aiohttp
        self._http_session = aiohttp.ClientSession()
        attempt = 0

        from martol_agent.base_wrapper import MAX_RECONNECT_ATTEMPTS, BASE_RECONNECT_DELAY, MAX_RECONNECT_DELAY
        import ssl
        import websockets
        from urllib.parse import urlparse

        while self._running and attempt < MAX_RECONNECT_ATTEMPTS:
            try:
                parsed = urlparse(self.ws_url)
                is_local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
                if not self.ws_url.startswith("wss://") and not is_local:
                    raise ValueError(
                        "WebSocket URL must use wss:// for non-local connections. "
                        "Use ws:// only for localhost development."
                    )
                if not self.mcp_url.startswith("https://") and not is_local:
                    raise ValueError(
                        "MCP URL must use https:// for non-local connections."
                    )

                ssl_context = ssl.create_default_context() if self.ws_url.startswith("wss://") else None

                url = f"{self.ws_url}?apiKey={self.api_key}"
                if self.last_known_id:
                    url += f"&lastKnownId={self.last_known_id}"

                async with websockets.connect(
                    url,
                    additional_headers={"x-api-key": self.api_key},
                    ssl=ssl_context,
                    open_timeout=10,
                    close_timeout=5,
                    max_size=1_048_576,
                    ping_interval=30,
                    ping_timeout=30,
                ) as ws:
                    self.ws = ws
                    attempt = 0
                    log.info("Connected to %s", self.ws_url)

                    if not self.agent_user_id:
                        await self._startup_sync()

                    await self._start_claude_session()
                    await self._listen(ws)

            except websockets.ConnectionClosedError as e:
                if e.code == 4001:
                    log.error("API key revoked (4001). Stopping permanently.")
                    break
                log.warning("Connection closed: %s", e)
            except Exception as e:
                log.warning("Connection error: %s", e)
            finally:
                await self._stop_claude_session()

            if not self._running:
                break

            attempt += 1
            delay = min(BASE_RECONNECT_DELAY * (2 ** (attempt - 1)), MAX_RECONNECT_DELAY)
            log.info("Reconnecting in %ds (attempt %d/%d)...", delay, attempt, MAX_RECONNECT_ATTEMPTS)
            await asyncio.sleep(delay)

        await self._shutdown()

    # ── Permission Handling ──────────────────────────────────────────

    async def _handle_permission(
        self,
        tool_name: str,
        input_data: dict,
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Relay permission requests to the chat room via action_submit."""
        # Hard-deny tools not in whitelist (if whitelist is configured)
        if self.claude_allowed_tools and not self._tool_allowed(tool_name):
            log.warning("Tool %s blocked by whitelist", tool_name)
            return PermissionResultDeny(message=f"Tool '{tool_name}' not in allowed list")

        # Format the tool call for human review
        if tool_name == "Bash":
            description = f"Run command: `{input_data.get('command', '')}`"
        elif tool_name == "Edit":
            file_path = input_data.get("file_path", "unknown")
            description = f"Edit file: `{file_path}`"
        elif tool_name == "Write":
            file_path = input_data.get("file_path", "unknown")
            description = f"Write file: `{file_path}`"
        else:
            description = f"Use tool: {tool_name}({json.dumps(input_data)[:200]})"

        # Determine risk level
        if tool_name == "Bash":
            risk = "high"
        elif tool_name in ("Write", "Edit", "NotebookEdit"):
            risk = "medium"
        else:
            risk = "low"

        # Post to chat room
        await self.send_message(
            f"**Permission request:** {description}\n"
            f"Submitting for approval (risk: {risk})..."
        )

        # Submit via MCP for approval
        # [C1] Include trigger_message_id — required by tool schema
        result = await self._mcp_call("action_submit", {
            "action_type": (
                "code_write" if tool_name in ("Write", "Edit", "NotebookEdit") else
                "code_review" if tool_name in ("Read", "Grep", "Glob", "LS") else
                "code_modify"
            ),
            "risk_level": risk,
            "trigger_message_id": self.last_known_id,
            "description": description,
            "payload": {"tool": tool_name, "input": input_data},
        })

        if result and result.get("ok"):
            action_id = result.get("data", {}).get("action_id")
            if action_id:
                # Poll for approval status
                approved = await self._wait_for_approval(action_id)
                if approved:
                    await self.send_message(f"Approved. Proceeding with: {description}")
                    return PermissionResultAllow(updated_input=input_data)
                else:
                    await self.send_message(f"Denied. Skipping: {description}")
                    return PermissionResultDeny(message="Action denied by room member")

        # If action_submit failed, deny by default
        log.warning("action_submit failed, denying tool %s", tool_name)
        return PermissionResultDeny(message="Could not submit for approval")

    def _tool_allowed(self, tool_name: str) -> bool:
        """Check if a tool is allowed by the whitelist. Supports wildcards (e.g. mcp__playwright__*)."""
        for pattern in self.claude_allowed_tools:
            if pattern.endswith("*"):
                if tool_name.startswith(pattern[:-1]):
                    return True
            elif tool_name == pattern:
                return True
        return False

    async def _wait_for_approval(self, action_id: int, timeout: float = 300) -> bool:
        """Poll action_status until approved, denied, or timeout."""
        start = time.monotonic()
        poll_interval = 3  # seconds

        while time.monotonic() - start < timeout:
            result = await self._mcp_call("action_status", {"action_id": action_id})
            if result and result.get("ok"):
                status = result.get("data", {}).get("status")
                if status == "approved":
                    return True
                elif status in ("denied", "rejected", "expired"):
                    return False
                # else: still pending, keep polling
            await asyncio.sleep(poll_interval)

        log.warning("Approval timeout for action %d", action_id)
        return False

    # ── Claude Code Prompt ───────────────────────────────────────────

    async def _send_to_claude(self, trigger: dict) -> None:
        """Send a chat message to Claude Code and relay the response."""
        async with self._responding:
            if not self.claude_client:
                log.warning("Claude Code session not active")
                return

            try:
                await self.send_typing(True)

                sender = trigger.get("senderName") or trigger.get("sender_name") or "unknown"
                body = trigger.get("body", "")
                prompt = f"[{sender}]: {body}"

                log.info("Sending to Claude Code (%d chars)", len(prompt))
                log.debug("Claude Code prompt: %s", prompt[:200])
                await self.claude_client.query(prompt)

                # Collect response text
                text_parts: list[str] = []
                async for message in self.claude_client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                text_parts.append(block.text)
                    elif isinstance(message, ResultMessage):
                        if message.is_error:
                            log.error("Claude Code error: %s", message.result)

                # Send collected text to chat room
                if text_parts:
                    full_text = "\n".join(text_parts)
                    # Chunk long responses (martol may have message size limits)
                    max_len = 4000
                    reply_to = trigger.get("serverSeqId") or trigger.get("id")
                    for i in range(0, len(full_text), max_len):
                        chunk = full_text[i:i + max_len]
                        await self.send_message(chunk, reply_to=reply_to)
                        reply_to = None  # Only first chunk replies to trigger

            except Exception as e:
                log.error("Failed to process with Claude Code: %s", e)
                log.debug("Full traceback:", exc_info=True)
            finally:
                await self.send_typing(False)
