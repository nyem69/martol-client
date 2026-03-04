#!/usr/bin/env python3
"""
Claude Code Bridge — connects a Claude Code session to a Martol chat room.

Instead of calling an LLM API directly, this wrapper manages a persistent
Claude Code subprocess via the Agent SDK. Chat messages become prompts,
Claude Code responses become chat messages, and tool permissions are
relayed to the room for approval via action_submit.
"""

import asyncio
import base64
import hashlib
import hmac as hmac_mod
import json
import logging
import os
import ssl
import time
import uuid
from typing import Any

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
except ImportError:
    print("Error: websockets required. pip install websockets")
    import sys
    sys.exit(1)

try:
    import aiohttp
except ImportError:
    print("Error: aiohttp required. pip install aiohttp")
    import sys
    sys.exit(1)

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

log = logging.getLogger("martol-agent")

# ── Configuration ────────────────────────────────────────────────────

MAX_RECONNECT_DELAY = 30
BASE_RECONNECT_DELAY = 1
MAX_RECONNECT_ATTEMPTS = 20

# Default safe tools if no whitelist specified
DEFAULT_SAFE_TOOLS = ["Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch"]


class ClaudeCodeWrapper:
    """Bridges a Martol chat room to a persistent Claude Code session."""

    def __init__(
        self,
        ws_url: str,
        api_key: str,
        mcp_url: str,
        context_size: int = 50,
        respond_mode: str = "mention",
        claude_model: str | None = None,
        claude_permission_mode: str = "default",
        claude_allowed_tools: list[str] | None = None,
        hmac_secret: str | None = None,
    ):
        self.ws_url = ws_url
        self.api_key = api_key
        self.mcp_url = mcp_url
        self.context_size = context_size
        self.respond_mode = respond_mode
        self.hmac_secret = hmac_secret
        self._hmac_warned = False
        self.claude_model = claude_model
        self.claude_permission_mode = claude_permission_mode
        self.claude_allowed_tools = claude_allowed_tools or []

        # Apply safe defaults if no whitelist specified
        if not self.claude_allowed_tools:
            log.info("No tool whitelist set — using safe defaults: %s", DEFAULT_SAFE_TOOLS)
            self.claude_allowed_tools = list(DEFAULT_SAFE_TOOLS)

        self.ws: WebSocketClientProtocol | None = None
        self.last_known_id = 0
        self.running = True
        self.agent_user_id: str | None = None
        self.agent_name: str | None = None
        self.room_name: str | None = None
        self.member_count: int = 0

        # Rolling conversation window (for mention detection context)
        self.conversation: list[dict] = []

        # Claude Code SDK client (persistent session)
        self.claude_client: ClaudeSDKClient | None = None

        # Lock to serialize prompts (one at a time)
        self._responding = asyncio.Lock()

    # ── Connection ───────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect with exponential backoff reconnection."""
        attempt = 0

        while self.running and attempt < MAX_RECONNECT_ATTEMPTS:
            try:
                # Enforce TLS in production
                if not self.ws_url.startswith("wss://") and "localhost" not in self.ws_url and "127.0.0.1" not in self.ws_url:
                    raise ValueError(f"Refusing non-TLS WebSocket URL: {self.ws_url}. Use wss:// for production.")

                ssl_context = ssl.create_default_context() if self.ws_url.startswith("wss://") else None

                url = f"{self.ws_url}?lastKnownId={self.last_known_id}"
                headers = {"x-api-key": self.api_key}

                log.info("Connecting to %s (attempt %d)...", self.ws_url, attempt + 1)
                async with websockets.connect(url, extra_headers=headers, ssl=ssl_context) as ws:
                    self.ws = ws
                    attempt = 0
                    log.info("Connected to room")

                    await self._startup_sync()
                    await self._start_claude_session()
                    await self._listen(ws)

            except websockets.ConnectionClosed as e:
                if e.code == 4001:
                    log.error("API key revoked (4001). Stopping.")
                    self.running = False
                    return
                log.warning("Connection closed: %s. Reconnecting...", e)
            except (ConnectionRefusedError, OSError) as e:
                log.warning("Connection failed: %s. Reconnecting...", e)
            except Exception as e:
                log.error("Unexpected error: %s", e, exc_info=True)
            finally:
                await self._stop_claude_session()

            if not self.running:
                break

            attempt += 1
            delay = min(
                BASE_RECONNECT_DELAY * (2 ** (attempt - 1)), MAX_RECONNECT_DELAY
            )
            log.info("Reconnecting in %.1fs...", delay)
            await asyncio.sleep(delay)

        if attempt >= MAX_RECONNECT_ATTEMPTS:
            log.error("Max reconnect attempts reached. Stopping.")

    # ── Startup ──────────────────────────────────────────────────────

    async def _startup_sync(self) -> None:
        """On connect, fetch room info and recent messages to seed context."""
        who = await self._mcp_call("chat_who", {})
        if who and who.get("ok"):
            data = who["data"]
            self.room_name = data.get("room_name", "unknown")
            self.agent_user_id = data.get("self_user_id")
            members = data.get("members", [])
            self.member_count = len(members)

            if self.agent_user_id:
                for m in members:
                    if m.get("user_id") == self.agent_user_id:
                        self.agent_name = m.get("name")
                        break

            log.info(
                "Room: %s (%d members), agent: %s (%s)",
                self.room_name,
                self.member_count,
                self.agent_name or "unknown",
                self.agent_user_id or "no id",
            )

        resync = await self._mcp_call("chat_resync", {"limit": self.context_size})
        if resync and resync.get("ok"):
            messages = resync["data"].get("messages", [])
            for msg in messages:
                self._append_context(msg)
            log.info("Loaded %d messages into context", len(messages))

        display = self.agent_name or "agent"
        model_display = self.claude_model or "default"
        await self.send_message(
            f"[AI Agent] {display} connected (powered by Claude Code, model: {model_display}). "
            f"I am an AI assistant with access to this project's codebase. "
            f"Responses should not be relied upon without verification."
        )

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
        result = await self._mcp_call("action_submit", {
            "action_type": (
                "code_write" if tool_name in ("Write", "Edit", "NotebookEdit") else
                "code_review" if tool_name in ("Read", "Grep", "Glob", "LS") else
                "code_modify"
            ),
            "risk_level": risk,
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

    # ── Listening ────────────────────────────────────────────────────

    async def _listen(self, ws: WebSocketClientProtocol) -> None:
        """Listen for incoming WebSocket messages."""
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Received non-JSON message, ignoring")
                continue
            if not self._verify_hmac(raw, msg):
                continue
            await self._handle_message(msg)

    def _verify_hmac(self, raw: str, msg: dict) -> bool:
        """Verify HMAC signature on server messages (R6)."""
        received_hmac = msg.pop("_hmac", None)
        if not self.hmac_secret:
            return True
        if received_hmac is None:
            if not self._hmac_warned:
                log.warning("Server message missing _hmac — server may not support R6 signing yet")
                self._hmac_warned = True
            return True
        raw_str = raw if isinstance(raw, str) else raw.decode()
        hmac_suffix = ',"_hmac":"' + received_hmac + '"}'
        if not raw_str.endswith(hmac_suffix):
            log.warning("Unexpected _hmac position in message, dropping")
            return False
        original_json = raw_str[: -len(hmac_suffix)] + "}"
        expected = hmac_mod.new(
            self.hmac_secret.encode(), original_json.encode("utf-8"), hashlib.sha256
        ).digest()
        try:
            received_bytes = base64.b64decode(received_hmac)
        except Exception:
            log.warning("Invalid _hmac encoding, dropping message")
            return False
        if not hmac_mod.compare_digest(expected, received_bytes):
            log.warning("HMAC verification failed, dropping message (type=%s)", msg.get("type"))
            return False
        return True

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        """Handle a server WebSocket message."""
        msg_type = msg.get("type")

        if msg_type == "message":
            payload = msg.get("message", {})
            seq_id = payload.get("serverSeqId", 0)
            if seq_id > self.last_known_id:
                self.last_known_id = seq_id

            sender = payload.get("senderName", "unknown")
            body = payload.get("body", "")
            role = payload.get("senderRole", "")

            log.debug("[%s/%s] %s", sender, role, body[:120])
            log.info("[%s/%s] message received (%d chars)", sender, role, len(body))

            self._append_context_from_ws(payload)

            if self._should_respond(payload):
                asyncio.create_task(self._send_to_claude(payload))

        elif msg_type == "history":
            messages = msg.get("messages", [])
            for m in messages:
                seq_id = m.get("serverSeqId", 0)
                if seq_id > self.last_known_id:
                    self.last_known_id = seq_id
                self._append_context_from_ws(m)
            log.info("Received %d history messages", len(messages))

        elif msg_type == "typing":
            pass

        elif msg_type == "presence":
            name = msg.get("senderName", "")
            status = msg.get("status", "")
            log.info("Presence: %s is %s", name, status)

        elif msg_type == "id_map":
            log.debug("ID mappings: %s", msg.get("mappings", []))

        elif msg_type == "error":
            code = msg.get("code", "")
            message = msg.get("message", "")
            log.error("Server error [%s]: %s", code, message)

    # ── Claude Code Prompt ───────────────────────────────────────────

    async def _send_to_claude(self, trigger: dict) -> None:
        """Send a chat message to Claude Code and relay the response."""
        async with self._responding:
            if not self.claude_client:
                log.warning("Claude Code session not active")
                return

            try:
                await self.send_typing(True)

                sender = trigger.get("senderName", "unknown")
                body = trigger.get("body", "")
                prompt = f"[{sender}]: {body}"

                log.info("Sending to Claude Code: %s", prompt[:120])
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
                log.error("Failed to process with Claude Code: %s", e, exc_info=True)
            finally:
                await self.send_typing(False)

    # ── Context Management ───────────────────────────────────────────

    def _append_context(self, msg: dict) -> None:
        """Append a message from MCP chat_read/resync format."""
        self.conversation.append({
            "id": msg.get("id", 0),
            "sender_id": msg.get("sender_id", ""),
            "sender_name": msg.get("sender_name", "unknown"),
            "sender_role": msg.get("sender_role", ""),
            "body": msg.get("body", ""),
            "reply_to": msg.get("reply_to"),
            "timestamp": msg.get("timestamp", ""),
        })
        if len(self.conversation) > self.context_size:
            self.conversation = self.conversation[-self.context_size:]

    def _append_context_from_ws(self, payload: dict) -> None:
        """Append a message from WebSocket format."""
        self.conversation.append({
            "id": payload.get("serverSeqId", 0),
            "sender_id": payload.get("senderId", ""),
            "sender_name": payload.get("senderName", "unknown"),
            "sender_role": payload.get("senderRole", ""),
            "body": payload.get("body", ""),
            "reply_to": payload.get("replyTo"),
            "timestamp": payload.get("createdAt", ""),
        })
        if len(self.conversation) > self.context_size:
            self.conversation = self.conversation[-self.context_size:]

    # ── Mention Detection ────────────────────────────────────────────

    def _should_respond(self, payload: dict) -> bool:
        """Decide whether to respond to a message."""
        sender_id = payload.get("senderId", "")

        if self.agent_user_id and sender_id == self.agent_user_id:
            return False

        if self.respond_mode == "all":
            return True

        body = payload.get("body", "")
        if self._is_mentioned(body):
            return True

        # Treat replies to the agent's own messages as implicit mentions
        reply_to = payload.get("replyTo")
        log.info("Mention check: mentioned=%s, replyTo=%r", False, reply_to)
        if reply_to:
            if self._is_reply_to_self(reply_to):
                return True

        return False

    def _is_reply_to_self(self, reply_to_id) -> bool:
        """Check if a replyTo ID refers to a message sent by this agent."""
        for msg in self.conversation:
            msg_id = msg.get("id")
            # Handle type mismatches (str vs int)
            if str(msg_id) == str(reply_to_id):
                is_self = msg.get("sender_id") == self.agent_user_id
                log.info("Reply target found: msg_id=%r, sender_id=%r, agent_id=%r, match=%s",
                         msg_id, msg.get("sender_id"), self.agent_user_id, is_self)
                return is_self
        log.info("Reply target %r not found in %d conversation messages", reply_to_id, len(self.conversation))
        return False

    def _is_mentioned(self, body: str) -> bool:
        """Check if the agent is mentioned in the message body."""
        if not self.agent_name:
            return False

        body_lower = body.lower()
        name_lower = self.agent_name.lower()

        if f"@{name_lower}" in body_lower:
            return True
        if name_lower in body_lower:
            return True

        return False

    # ── MCP HTTP Client ──────────────────────────────────────────────

    async def _mcp_call(self, tool: str, params: dict) -> dict | None:
        """Call the MCP HTTP endpoint."""
        url = f"{self.mcp_url}/mcp/v1"
        headers = {
            "x-api-key": self.api_key,
            "content-type": "application/json",
        }
        body = {"tool": tool, "params": params}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=body, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    result = await resp.json()
                    if not result.get("ok"):
                        log.warning("MCP %s failed: %s", tool, result.get("error", "unknown"))
                    return result
        except Exception as e:
            log.error("MCP call %s failed: %s", tool, e)
            return None

    # ── WebSocket Send ───────────────────────────────────────────────

    async def send_message(self, body: str, reply_to: int | None = None) -> bool:
        """Send a chat message via WebSocket."""
        if not self.ws:
            log.warning("Not connected, cannot send")
            return False

        local_id = uuid.uuid4().hex
        payload: dict[str, Any] = {
            "type": "message",
            "body": body,
            "localId": local_id,
        }
        if reply_to is not None:
            payload["replyTo"] = reply_to

        try:
            await self.ws.send(json.dumps(payload))
            log.debug("Sent message: %s", body[:80])
            log.info("Sent message (%d chars)", len(body))
            return True
        except Exception as e:
            log.error("Failed to send: %s", e)
            return False

    async def send_typing(self, active: bool = True) -> None:
        """Send typing indicator."""
        if not self.ws:
            return
        try:
            await self.ws.send(json.dumps({"type": "typing", "active": active}))
        except Exception:
            pass

    def stop(self) -> None:
        """Gracefully stop the wrapper."""
        log.info("Stopping Claude Code wrapper...")
        self.running = False
        if self.ws:
            asyncio.ensure_future(self._shutdown())

    async def _shutdown(self) -> None:
        """Send farewell message, stop Claude Code, and close WebSocket."""
        try:
            await self.send_message("[AI Agent] Disconnecting. Goodbye!")
            await self._stop_claude_session()
            if self.ws:
                await self.ws.close()
        except Exception:
            pass
