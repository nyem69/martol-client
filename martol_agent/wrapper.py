#!/usr/bin/env python3
"""
Martol Agent Wrapper — connects an AI agent to a chat room via WebSocket + MCP HTTP.

Dual channel architecture:
  - WebSocket: real-time listen + send messages + typing indicators
  - MCP HTTP (/mcp/v1): action_submit, action_status, chat_read, chat_resync

Usage:
    python -m martol_agent --url wss://martol.plitix.com/api/rooms/<roomId>/ws \
        --api-key <martol-key> --provider anthropic --ai-key <ai-key>

Environment variables (alternative to flags):
    MARTOL_WS_URL    — WebSocket URL
    MARTOL_API_KEY   — Agent API key
    MARTOL_MCP_URL   — MCP HTTP endpoint base (derived from WS URL if omitted)
    AI_PROVIDER      — "anthropic" or "openai"
    AI_API_KEY       — LLM provider API key
    AI_MODEL         — Model ID override
    AI_BASE_URL      — OpenAI-compatible base URL
    CONTEXT_MESSAGES — Rolling context window size (default: 50)
    RESPOND_MODE     — "mention" (only @mentions) or "all" (every non-own message)
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
except ImportError:
    print("Error: websockets required. pip install websockets")
    sys.exit(1)

try:
    import aiohttp
except ImportError:
    print("Error: aiohttp required. pip install aiohttp")
    sys.exit(1)

from martol_agent.providers import LLMProvider, LLMResponse, create_provider
from martol_agent.tools import TOOLS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("martol-agent")

# ── Configuration ────────────────────────────────────────────────────

MAX_RECONNECT_DELAY = 30
BASE_RECONNECT_DELAY = 1
MAX_RECONNECT_ATTEMPTS = 20
MAX_TOOL_ITERATIONS = 5


def derive_mcp_url(ws_url: str) -> str:
    """Derive MCP HTTP base URL from the WebSocket URL.

    wss://martol.plitix.com/api/rooms/<id>/ws -> https://martol.plitix.com
    """
    parsed = urlparse(ws_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return f"{scheme}://{parsed.hostname}" + (
        f":{parsed.port}" if parsed.port else ""
    )


class AgentWrapper:
    """Connects to a Martol chat room and relays messages to an LLM."""

    def __init__(
        self,
        ws_url: str,
        api_key: str,
        provider: LLMProvider,
        mcp_url: str,
        context_size: int = 50,
        respond_mode: str = "mention",
    ):
        self.ws_url = ws_url
        self.api_key = api_key
        self.provider = provider
        self.mcp_url = mcp_url
        self.context_size = context_size
        self.respond_mode = respond_mode

        self.ws: WebSocketClientProtocol | None = None
        self.last_known_id = 0
        self.running = True
        self.agent_user_id: str | None = None
        self.agent_name: str | None = None
        self.room_name: str | None = None
        self.member_count: int = 0

        # Rolling conversation window for LLM context
        self.conversation: list[dict] = []

        # Lock to serialize LLM calls (one response at a time)
        self._responding = asyncio.Lock()

    # ── Connection ───────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect with exponential backoff reconnection."""
        attempt = 0

        while self.running and attempt < MAX_RECONNECT_ATTEMPTS:
            try:
                url = f"{self.ws_url}?lastKnownId={self.last_known_id}"
                headers = {"x-api-key": self.api_key}

                log.info("Connecting to %s (attempt %d)...", self.ws_url, attempt + 1)
                async with websockets.connect(url, extra_headers=headers) as ws:
                    self.ws = ws
                    attempt = 0
                    log.info("Connected to room")

                    # Startup: fetch room info + recent history
                    await self._startup_sync()
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

    async def _startup_sync(self) -> None:
        """On connect, fetch room info and recent messages to seed context."""
        # Get room info
        who = await self._mcp_call("chat_who", {})
        if who and who.get("ok"):
            data = who["data"]
            self.room_name = data.get("room_name", "unknown")
            self.agent_user_id = data.get("self_user_id")
            members = data.get("members", [])
            self.member_count = len(members)

            # Resolve our display name from the member list
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

        # Resync recent messages (does NOT trigger responses)
        resync = await self._mcp_call("chat_resync", {"limit": self.context_size})
        if resync and resync.get("ok"):
            messages = resync["data"].get("messages", [])
            for msg in messages:
                self._append_context(msg)
            log.info("Loaded %d messages into context", len(messages))

        # AI disclosure: announce presence per Anthropic usage policy
        provider_name = type(self.provider).__name__.replace("Provider", "")
        model_name = getattr(self.provider, "model", "unknown")
        display = self.agent_name or "agent"
        await self.send_message(
            f"[AI Agent] {display} connected (powered by {provider_name}, model: {model_name}). "
            f"I am an AI assistant. Responses should not be relied upon without verification."
        )

    # ── Listening ────────────────────────────────────────────────────

    async def _listen(self, ws: WebSocketClientProtocol) -> None:
        """Listen for incoming WebSocket messages."""
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Received non-JSON message, ignoring")
                continue
            await self._handle_message(msg)

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

            log.info("[%s/%s] %s", sender, role, body[:120])

            # Append to context window
            self._append_context_from_ws(payload)

            # Check if we should respond
            if self._should_respond(payload):
                asyncio.create_task(self._generate_response(payload))

        elif msg_type == "history":
            messages = msg.get("messages", [])
            for m in messages:
                seq_id = m.get("serverSeqId", 0)
                if seq_id > self.last_known_id:
                    self.last_known_id = seq_id
                self._append_context_from_ws(m)
            log.info("Received %d history messages", len(messages))

        elif msg_type == "id_map":
            log.debug("ID mappings: %s", msg.get("mappings", []))

        elif msg_type == "typing":
            pass

        elif msg_type == "presence":
            name = msg.get("senderName", "")
            status = msg.get("status", "")
            log.info("Presence: %s is %s", name, status)

        elif msg_type == "error":
            code = msg.get("code", "")
            message = msg.get("message", "")
            log.error("Server error [%s]: %s", code, message)

    # ── Context Management ───────────────────────────────────────────

    def _append_context(self, msg: dict) -> None:
        """Append a message from MCP chat_read/resync format to the context window."""
        self.conversation.append(
            {
                "id": msg.get("id", 0),
                "sender_id": msg.get("sender_id", ""),
                "sender_name": msg.get("sender_name", "unknown"),
                "sender_role": msg.get("sender_role", ""),
                "body": msg.get("body", ""),
                "reply_to": msg.get("reply_to"),
                "timestamp": msg.get("timestamp", ""),
            }
        )
        if len(self.conversation) > self.context_size:
            self.conversation = self.conversation[-self.context_size :]

    def _append_context_from_ws(self, payload: dict) -> None:
        """Append a message from WebSocket format to the context window."""
        self.conversation.append(
            {
                "id": payload.get("serverSeqId", 0),
                "sender_id": payload.get("senderId", ""),
                "sender_name": payload.get("senderName", "unknown"),
                "sender_role": payload.get("senderRole", ""),
                "body": payload.get("body", ""),
                "reply_to": payload.get("replyTo"),
                "timestamp": payload.get("createdAt", ""),
            }
        )
        if len(self.conversation) > self.context_size:
            self.conversation = self.conversation[-self.context_size :]

    # ── Mention Detection ────────────────────────────────────────────

    def _should_respond(self, payload: dict) -> bool:
        """Decide whether to respond to a message."""
        sender_id = payload.get("senderId", "")

        # Never respond to own messages
        if self.agent_user_id and sender_id == self.agent_user_id:
            return False

        # In "all" mode, respond to every non-own message
        if self.respond_mode == "all":
            return True

        # In "mention" mode, check for @label or @name
        body = payload.get("body", "")
        return self._is_mentioned(body)

    def _is_mentioned(self, body: str) -> bool:
        """Check if the agent is mentioned in the message body."""
        if not self.agent_name:
            return False

        body_lower = body.lower()
        name_lower = self.agent_name.lower()

        # Check @name (e.g. @claude:backend)
        if f"@{name_lower}" in body_lower:
            return True

        # Check without @ prefix (just the name)
        if name_lower in body_lower:
            return True

        return False

    # ── Response Generation ──────────────────────────────────────────

    async def _generate_response(self, trigger: dict) -> None:
        """Generate and send an LLM response."""
        async with self._responding:
            try:
                await self.send_typing(True)

                system = self._build_system_prompt()
                messages = self._build_llm_messages()

                log.info("Calling LLM with %d context messages...", len(messages))
                response = await self.provider.chat(system, messages, TOOLS)

                await self._process_response(response, trigger)

            except Exception as e:
                log.error("Failed to generate response: %s", e, exc_info=True)
            finally:
                await self.send_typing(False)

    def _build_system_prompt(self) -> str:
        """Build the system prompt with room context."""
        display = self.agent_name or "agent"
        return (
            f"You are {display}, an AI assistant in a collaborative workspace "
            f"called Martol.\n"
            f'You are in room "{self.room_name or "unknown"}" with '
            f"{self.member_count} members.\n\n"
            f"You respond when mentioned with @{display}.\n\n"
            f"When users ask you to take actions (write code, modify files, deploy, "
            f"review code, etc.), use the action_submit tool. The action will be "
            f"reviewed and approved by a human with sufficient authority before "
            f"being executed.\n\n"
            f"For simple questions and conversation, respond directly without tools.\n"
            f"Keep responses concise and relevant to the discussion."
        )

    def _build_llm_messages(self) -> list[dict]:
        """Build LLM messages from the conversation context.

        Maps chat messages to user/assistant roles based on sender.
        Groups consecutive same-role messages.
        """
        messages: list[dict] = []

        for msg in self.conversation:
            sender_id = msg.get("sender_id", "")
            sender_name = msg.get("sender_name", "unknown")
            body = msg.get("body", "")

            if not body.strip():
                continue

            is_self = self.agent_user_id and sender_id == self.agent_user_id

            if is_self:
                role = "assistant"
                content = body
            else:
                role = "user"
                content = f"[{sender_name}]: {body}"

            # Merge consecutive same-role messages
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += f"\n{content}"
            else:
                messages.append({"role": role, "content": content})

        # Ensure messages start with "user" role (required by both APIs)
        if messages and messages[0]["role"] != "user":
            messages = messages[1:]

        # Ensure we have at least one message
        if not messages:
            messages = [{"role": "user", "content": "(no recent messages)"}]

        return messages

    async def _process_response(
        self, response: LLMResponse, trigger: dict
    ) -> None:
        """Process LLM response: send text and/or execute tool calls."""
        iteration = 0

        while iteration < MAX_TOOL_ITERATIONS:
            # Send any text content as a chat message
            if response.text and response.text.strip():
                reply_to = trigger.get("serverSeqId") or trigger.get("id")
                await self.send_message(response.text.strip(), reply_to=reply_to)

            # If no tool calls, we're done
            if not response.tool_calls:
                break

            # Execute tool calls via MCP HTTP
            tool_results: list[dict] = []
            for tc in response.tool_calls:
                log.info("Executing tool: %s(%s)", tc.name, tc.arguments)
                result = await self._mcp_call(tc.name, tc.arguments)
                log.info("Tool result: %s", json.dumps(result)[:200])
                tool_results.append({"tool_call": tc, "result": result})

            # Build follow-up messages with tool results
            follow_up = self._build_tool_result_messages(response, tool_results)
            if not follow_up:
                break

            # Call LLM again with tool results
            system = self._build_system_prompt()
            messages = self._build_llm_messages()
            messages.extend(follow_up)

            log.info("Calling LLM with tool results (iteration %d)...", iteration + 1)
            response = await self.provider.chat(system, messages, TOOLS)
            iteration += 1

        if iteration >= MAX_TOOL_ITERATIONS:
            log.warning("Hit max tool iterations (%d), stopping", MAX_TOOL_ITERATIONS)

    def _build_tool_result_messages(
        self, response: LLMResponse, tool_results: list[dict]
    ) -> list[dict]:
        """Build provider-specific tool result messages."""
        from martol_agent.providers.anthropic import AnthropicProvider
        from martol_agent.providers.openai_compat import OpenAICompatProvider

        messages: list[dict] = []

        if isinstance(self.provider, AnthropicProvider):
            # Anthropic: assistant message with tool_use blocks, then user
            # message with tool_result blocks
            messages.append(
                AnthropicProvider.format_assistant_message(response)
            )
            results = []
            for tr in tool_results:
                results.append(
                    AnthropicProvider.format_tool_result(
                        tr["tool_call"].id, tr["result"] or {"ok": False}
                    )
                )
            messages.append({"role": "user", "content": results})

        elif isinstance(self.provider, OpenAICompatProvider):
            # OpenAI: assistant message with tool_calls, then tool messages
            messages.append(
                OpenAICompatProvider.format_assistant_message(response)
            )
            for tr in tool_results:
                messages.append(
                    OpenAICompatProvider.format_tool_result(
                        tr["tool_call"].id, tr["result"] or {"ok": False}
                    )
                )

        return messages

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
                    url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    result = await resp.json()
                    if not result.get("ok"):
                        log.warning(
                            "MCP %s failed: %s",
                            tool,
                            result.get("error", "unknown"),
                        )
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
            log.info("Sent message: %s", body[:80])
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
        log.info("Stopping agent wrapper...")
        self.running = False
        if self.ws:
            asyncio.ensure_future(self._shutdown())

    async def _shutdown(self) -> None:
        """Send farewell message and close WebSocket cleanly."""
        try:
            await self.send_message("[AI Agent] Disconnecting. Goodbye!")
            if self.ws:
                await self.ws.close()
        except Exception:
            pass


# ── CLI ──────────────────────────────────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(description="Martol Agent Wrapper")
    parser.add_argument(
        "--url", default=os.environ.get("MARTOL_WS_URL"), help="WebSocket URL"
    )
    parser.add_argument(
        "--api-key", default=os.environ.get("MARTOL_API_KEY"), help="Martol API key"
    )
    parser.add_argument(
        "--mcp-url",
        default=os.environ.get("MARTOL_MCP_URL"),
        help="MCP HTTP base URL (derived from WS URL if omitted)",
    )
    parser.add_argument(
        "--provider",
        default=os.environ.get("AI_PROVIDER", "anthropic"),
        choices=["anthropic", "openai"],
        help="LLM provider",
    )
    parser.add_argument(
        "--ai-key", default=os.environ.get("AI_API_KEY"), help="LLM provider API key"
    )
    parser.add_argument(
        "--model", default=os.environ.get("AI_MODEL"), help="Model ID override"
    )
    parser.add_argument(
        "--ai-base-url",
        default=os.environ.get("AI_BASE_URL"),
        help="OpenAI-compatible base URL",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=int(os.environ.get("CONTEXT_MESSAGES", "50")),
        help="Rolling context window size",
    )
    parser.add_argument(
        "--respond",
        default=os.environ.get("RESPOND_MODE", "mention"),
        choices=["mention", "all"],
        help="Response mode: mention (only @mentions) or all",
    )
    args = parser.parse_args()

    # Validate required args
    if not args.url:
        print("Error: WebSocket URL required (--url or MARTOL_WS_URL)")
        sys.exit(1)
    if not args.api_key:
        print("Error: Martol API key required (--api-key or MARTOL_API_KEY)")
        sys.exit(1)
    if not args.ai_key:
        print("Error: LLM API key required (--ai-key or AI_API_KEY)")
        sys.exit(1)

    # Derive MCP URL if not provided
    mcp_url = args.mcp_url or derive_mcp_url(args.url)

    # Create LLM provider
    provider = create_provider(
        provider=args.provider,
        api_key=args.ai_key,
        model=args.model,
        base_url=args.ai_base_url,
    )
    log.info("Using provider: %s (model: %s)", args.provider, args.model or "default")

    # Create wrapper
    wrapper = AgentWrapper(
        ws_url=args.url,
        api_key=args.api_key,
        provider=provider,
        mcp_url=mcp_url,
        context_size=args.context,
        respond_mode=args.respond,
    )

    # Handle graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, wrapper.stop)

    log.info(
        "Starting agent (mode=%s, context=%d, mcp=%s)",
        args.respond,
        args.context,
        mcp_url,
    )
    await wrapper.connect()


if __name__ == "__main__":
    asyncio.run(main())
