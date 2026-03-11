"""Base wrapper with shared WebSocket, MCP, and message handling logic."""

import asyncio
import hashlib
import hmac as hmac_lib
import json
import logging
import os
import re
import ssl
import stat
import uuid
from base64 import b64decode
from urllib.parse import urlparse

import aiohttp
import websockets

log = logging.getLogger("martol-agent")

MAX_RECONNECT_DELAY = 30
BASE_RECONNECT_DELAY = 1
MAX_RECONNECT_ATTEMPTS = 20


def derive_mcp_url(ws_url: str) -> str:
    """Derive MCP HTTP base URL from WebSocket URL."""
    parsed = urlparse(ws_url)
    if not parsed.hostname:
        raise ValueError(f"Cannot derive MCP URL: no hostname in {ws_url}")
    scheme = "https" if parsed.scheme == "wss" else "http"
    port = f":{parsed.port}" if parsed.port else ""
    # Bracket IPv6 addresses for valid URI form (e.g. http://[::1]:3000)
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{scheme}://{host}{port}"


class BaseWrapper:
    """Shared logic for WebSocket + MCP communication with martol server."""

    def __init__(
        self,
        ws_url: str,
        api_key: str,
        mcp_url: str | None = None,
        context_size: int = 50,
        respond_mode: str = "mention",
        hmac_secret: str | None = None,
        allow_unsigned: bool = False,
    ):
        self.ws_url = ws_url
        self.api_key = api_key
        self.mcp_url = mcp_url or derive_mcp_url(ws_url)
        self.context_size = context_size
        self.respond_mode = respond_mode
        self.hmac_secret = hmac_secret.encode() if hmac_secret else None
        self.allow_unsigned = allow_unsigned

        self.conversation: list[dict] = []
        self.agent_user_id: str | None = None
        self.agent_name: str | None = None
        self.room_name: str = "unknown"
        self.room_id: str | None = None  # org/room ID for MCP context
        self.room_brief: str | None = None
        self.room_brief_version: int = 0
        self.last_known_id: int = 0
        self._responding = asyncio.Lock()
        self._running = True
        self.ws = None

        # Reply-detection index (separate from LLM context window)
        self._message_index: dict[str, dict] = {}
        self._message_index_order: list[str] = []
        self._MESSAGE_INDEX_MAX = 200

        # Seen seqIds for duplicate detection
        self._seen_seq_ids: set[int] = set()
        self._SEEN_SEQ_MAX = 500

        # Users who opted out of AI context (HI-04)
        self._ai_opt_out_users: set[str] = set()

        # ID mapping: serverSeqId -> dbId
        self._id_map: dict[str, str] = {}

        # Persistent HTTP session for MCP calls
        self._http_session: aiohttp.ClientSession | None = None

    # --- Connection ---

    async def connect(self):
        """Connect to WebSocket with reconnection logic."""
        self._http_session = aiohttp.ClientSession()
        attempt = 0
        while self._running and attempt < MAX_RECONNECT_ATTEMPTS:
            try:
                parsed = urlparse(self.ws_url)
                is_local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
                if not self.ws_url.startswith("wss://") and not is_local:
                    raise ValueError(
                        "WebSocket URL must use wss:// for non-local connections. "
                        "Use ws:// only for localhost development."
                    )
                # Enforce TLS for MCP URL too
                if not self.mcp_url.startswith("https://") and not is_local:
                    raise ValueError(
                        "MCP URL must use https:// for non-local connections."
                    )

                ssl_context = ssl.create_default_context() if self.ws_url.startswith("wss://") else None

                url = self.ws_url
                if self.last_known_id:
                    url += f"?lastKnownId={self.last_known_id}"

                async with websockets.connect(
                    url,
                    extra_headers={"x-api-key": self.api_key},
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

                    await self._on_connected()
                    await self._listen(ws)

            except websockets.ConnectionClosedOK:
                if not self._running:
                    break
                log.info("Connection closed normally")
            except websockets.ConnectionClosedError as e:
                if e.code == 4001:
                    log.error("API key revoked (4001). Stopping permanently.")
                    break
                if not self._running:
                    break
                log.warning("Connection closed: %s", e)
            except (asyncio.CancelledError, KeyboardInterrupt):
                break
            except (
                websockets.exceptions.InvalidStatus,
                websockets.legacy.exceptions.InvalidStatusCode,
            ) as e:
                if not self._running:
                    break
                code = getattr(e, 'status_code', None) or getattr(e.response, 'status_code', 0)
                hints = {
                    401: "API key is invalid, expired, or revoked. Check MARTOL_API_KEY.",
                    403: "Access denied — not a member of this room, or origin not allowed.",
                    400: "Bad request — check the WebSocket URL format.",
                    503: "Server unavailable — database or signing key may be down.",
                }
                hint = hints.get(code, "")
                log.error("Connection rejected: HTTP %d. %s", code, hint)
                if code == 401:
                    log.error("Stopping — fix the API key and restart.")
                    break
            except Exception as e:
                if not self._running:
                    break
                log.warning("Connection error: %s", e)

            if not self._running:
                break

            attempt += 1
            delay = min(BASE_RECONNECT_DELAY * (2 ** (attempt - 1)), MAX_RECONNECT_DELAY)
            log.info("Reconnecting in %ds (attempt %d/%d)...", delay, attempt, MAX_RECONNECT_ATTEMPTS)
            await asyncio.sleep(delay)

        try:
            await self._on_disconnected()
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await self._shutdown()
        except (asyncio.CancelledError, Exception):
            pass

    async def _startup_sync(self):
        """Resolve identity and seed context from server."""
        who = await self._mcp_call("chat_who", {})
        if who and who.get("ok"):
            data = who.get("data", {})
            self.room_name = data.get("room_name", "unknown")
            self.room_id = data.get("room_id")
            self.room_brief = data.get("brief")
            self.room_brief_version = data.get("brief_version", 0)
            self.agent_user_id = data.get("self_user_id")
            members = data.get("members", [])
            for m in members:
                if m.get("user_id") == self.agent_user_id:
                    self.agent_name = m.get("name", "agent")
                    break
            humans = [m for m in members if not m.get("is_agent")]
            agents = [m for m in members if m.get("is_agent")]

            # Parse AI opt-out preferences (HI-04)
            self._ai_opt_out_users = {
                m["user_id"] for m in members
                if m.get("ai_opt_out", False) and m.get("user_id")
            }
            if self._ai_opt_out_users:
                log.info("AI opt-out: %d user(s) excluded from context", len(self._ai_opt_out_users))

            log.info("Identity: %s (id=%s) in room '%s' (%d humans, %d agents)",
                     self.agent_name, self.agent_user_id, self.room_name,
                     len(humans), len(agents))
        else:
            log.error("Failed to resolve identity via chat_who")

        if not self.agent_user_id:
            log.error("Cannot resolve agent identity. Refusing to start (would cause self-response loops).")
            self._running = False
            return

        resync = await self._mcp_call("chat_resync", {"limit": self.context_size})
        if resync and resync.get("ok"):
            messages = resync.get("data", {}).get("messages", [])
            for msg in messages:
                self._append_context(msg)
            log.info("Loaded %d messages from chat_resync", len(messages))

        await self._send_disclosure()

    async def _on_connected(self):
        """Called after WebSocket connection and startup sync. Override in subclasses."""
        pass

    async def _on_disconnected(self):
        """Called during shutdown. Override in subclasses."""
        pass

    async def _send_disclosure(self):
        """Send AI disclosure message. Override in subclasses for custom text."""
        raise NotImplementedError

    # --- WebSocket Listening ---

    async def _listen(self, ws):
        """Listen for WebSocket messages."""
        try:
            async for raw in ws:
                if not self._running:
                    break
                try:
                    raw_str = raw if isinstance(raw, str) else raw.decode()
                    msg = json.loads(raw_str)
                    await self._handle_message(msg, raw_str)
                except json.JSONDecodeError:
                    log.warning("Invalid JSON from WebSocket")
                except Exception as e:
                    log.error("Error handling message: %s", e)
                    log.debug("Full traceback:", exc_info=True)
        except asyncio.CancelledError:
            return

    # --- HMAC Verification ---

    def _verify_hmac(self, raw_json: str, msg: dict) -> bool:
        """Verify HMAC signature on server messages."""
        if not self.hmac_secret:
            return True

        received_hmac = msg.get("_hmac")
        if received_hmac is None:
            if self.allow_unsigned:
                log.warning("Server message missing _hmac — allowing (--allow-unsigned is set)")
                return True
            log.warning("Server message missing _hmac — dropping unsigned message")
            return False

        try:
            hmac_bytes = b64decode(received_hmac)
        except Exception:
            log.warning("Invalid _hmac base64 encoding")
            return False

        # Reconstruct original JSON (before _hmac was appended)
        suffix = ',"_hmac":"' + received_hmac + '"}'
        if raw_json.endswith(suffix):
            original = raw_json[: -len(suffix)] + "}"
        else:
            log.warning("Cannot reconstruct original JSON for HMAC verification")
            return False

        expected = hmac_lib.new(self.hmac_secret, original.encode(), hashlib.sha256).digest()
        if not hmac_lib.compare_digest(hmac_bytes, expected):
            log.warning("HMAC verification failed — message rejected")
            return False

        return True

    # --- Message Handling ---

    async def _handle_message(self, msg: dict, raw_json: str = ""):
        """Route incoming WebSocket messages."""
        msg_type = msg.get("type")

        if msg_type == "message":
            if not self._verify_hmac(raw_json, msg):
                return

            payload = msg.get("message", msg)
            sender_id = payload.get("sender_id") or payload.get("senderId")
            sender = payload.get("sender_name") or payload.get("senderName") or "unknown"
            role = payload.get("sender_role") or payload.get("senderRole") or "unknown"
            body = payload.get("body", "")

            log.debug("[%s/%s] %s", sender, role, body[:120])

            self._append_context_from_ws(payload)

            if self.agent_user_id and sender_id == self.agent_user_id:
                return

            if self._should_respond(payload):
                await self._on_trigger(payload)

        elif msg_type == "history":
            messages = msg.get("messages", [])
            for m in messages:
                self._append_context_from_ws(m)
            log.info("Delta sync: %d messages", len(messages))

        elif msg_type == "id_map":
            for mapping in msg.get("mappings", []):
                server_seq = str(mapping.get("serverSeqId", ""))
                db_id = str(mapping.get("dbId", ""))
                if server_seq and db_id:
                    self._id_map[server_seq] = db_id
            # Prune if too large
            if len(self._id_map) > self._SEEN_SEQ_MAX:
                keys = sorted(self._id_map.keys(), key=lambda k: int(k) if k.isdigit() else 0)
                for k in keys[:len(keys) // 2]:
                    del self._id_map[k]

        elif msg_type == "error":
            code = msg.get("code", "")
            log.warning("Server error: %s — %s", code, msg.get("message", ""))
            if code == "resync_required":
                await self._do_resync()

    async def _do_resync(self):
        """Re-seed conversation context and refresh brief from server."""
        log.info("Resyncing conversation context...")
        self.conversation.clear()
        self._seen_seq_ids.clear()
        self._message_index.clear()
        self._message_index_order.clear()
        self.last_known_id = 0

        resync = await self._mcp_call("chat_resync", {"limit": self.context_size})
        if resync and resync.get("ok"):
            messages = resync.get("data", {}).get("messages", [])
            for msg in messages:
                self._append_context(msg)
            log.info("Resync complete: %d messages loaded", len(messages))
        else:
            log.error("Resync failed: %s", resync)

        # Re-fetch brief to detect updates since last sync
        brief = await self._mcp_call("brief_get_active", {})
        if brief and brief.get("ok"):
            data = brief.get("data", {})
            new_version = data.get("version", 0)
            if new_version != self.room_brief_version:
                log.info("Brief updated: v%d → v%d", self.room_brief_version, new_version)
                self.room_brief = data.get("brief")
                self.room_brief_version = new_version

    async def _on_trigger(self, payload: dict):
        """Called when a message should trigger a response. Override in subclasses."""
        raise NotImplementedError

    # --- Context Management ---

    def _append_context(self, msg: dict):
        """Append a message from MCP format (chat_resync) to conversation."""
        entry = {
            "id": msg.get("id", 0),
            "sender_id": msg.get("senderId") or msg.get("sender_id"),
            "sender_name": msg.get("senderName") or msg.get("sender_name") or "unknown",
            "sender_role": msg.get("senderRole") or msg.get("sender_role") or "unknown",
            "body": msg.get("body", ""),
            "type": msg.get("type", "message"),
        }
        self.conversation.append(entry)
        if len(self.conversation) > self.context_size:
            self.conversation = self.conversation[-self.context_size:]

        # Also index for reply detection
        msg_id = str(entry["id"])
        self._index_message(msg_id, entry)

    def _append_context_from_ws(self, payload: dict):
        """Append a message from WebSocket format to conversation."""
        seq_id = payload.get("serverSeqId", 0)

        # Duplicate detection
        if seq_id and seq_id in self._seen_seq_ids:
            return
        if seq_id:
            self._seen_seq_ids.add(seq_id)
            if len(self._seen_seq_ids) > self._SEEN_SEQ_MAX:
                # Prune oldest half
                sorted_ids = sorted(self._seen_seq_ids)
                self._seen_seq_ids = set(sorted_ids[len(sorted_ids) // 2:])

        entry = {
            "id": seq_id,
            "sender_id": payload.get("sender_id") or payload.get("senderId"),
            "sender_name": payload.get("sender_name") or payload.get("senderName") or "unknown",
            "sender_role": payload.get("sender_role") or payload.get("senderRole") or "unknown",
            "body": payload.get("body", ""),
            "type": payload.get("type", "message"),
            "replyTo": payload.get("replyTo"),
        }
        self.conversation.append(entry)
        if len(self.conversation) > self.context_size:
            self.conversation = self.conversation[-self.context_size:]

        if self.last_known_id < seq_id:
            self.last_known_id = seq_id

        # Also index for reply detection
        msg_id = str(seq_id)
        self._index_message(msg_id, entry)

    def _index_message(self, msg_id: str, entry: dict):
        """Add to reply-detection index (separate from LLM context window)."""
        if msg_id and msg_id != "0":
            self._message_index[msg_id] = entry
            self._message_index_order.append(msg_id)
            if len(self._message_index_order) > self._MESSAGE_INDEX_MAX:
                old_id = self._message_index_order.pop(0)
                self._message_index.pop(old_id, None)

    # --- Response Gating ---

    def _should_respond(self, payload: dict) -> bool:
        """Check if the agent should respond to this message."""
        if self.respond_mode == "all":
            return True

        body = payload.get("body", "")
        if self._is_mentioned(body):
            return True

        reply_to = payload.get("replyTo")
        if reply_to:
            if self._is_reply_to_self(reply_to):
                return True

        return False

    def _is_reply_to_self(self, reply_to_id) -> bool:
        """Check if reply target is a message sent by this agent."""
        reply_key = str(reply_to_id)

        # Check reply-detection index first (larger window)
        if reply_key in self._message_index:
            entry = self._message_index[reply_key]
            return entry.get("sender_id") == self.agent_user_id

        # Fallback to conversation window
        for msg in self.conversation:
            if str(msg.get("id")) == reply_key:
                return msg.get("sender_id") == self.agent_user_id

        return False

    def _is_mentioned(self, body: str) -> bool:
        """Check if the agent is mentioned in the message body."""
        if re.search(r'\b@all\b', body, re.IGNORECASE):
            return True
        if not self.agent_name:
            return False
        name = self.agent_name
        # Word boundary match to avoid false positives
        if re.search(rf'\b@?{re.escape(name)}\b', body, re.IGNORECASE):
            return True
        return False

    # --- MCP HTTP ---

    async def _mcp_call(self, tool: str, params: dict) -> dict | None:
        """Call an MCP tool via HTTP."""
        if not self._http_session:
            self._http_session = aiohttp.ClientSession()

        url = f"{self.mcp_url}/mcp/v1"
        body = {"tool": tool, "params": params}
        headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}
        if self.room_id:
            headers["x-org-id"] = self.room_id

        try:
            async with self._http_session.post(
                url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
            ) as resp:
                if resp.status >= 500:
                    log.error("MCP %s server error: HTTP %d", tool, resp.status)
                    return None
                if resp.status == 301 or resp.status == 302:
                    log.error("MCP %s unexpected redirect: %d", tool, resp.status)
                    return None
                return await resp.json()
        except Exception as e:
            log.error("MCP call %s failed: %s", tool, e)
            return None

    # --- Sending ---

    async def send_message(self, body: str, reply_to: int | None = None) -> bool:
        """Send a chat message via WebSocket."""
        if not self.ws:
            return False
        # Client-side body size check (server caps at 32KB)
        if len(body.encode("utf-8")) > 32768:
            log.warning("Message body exceeds 32KB, truncating")
            body = body[:32000] + "\n[truncated]"
        msg = {
            "type": "message",
            "body": body,
            "localId": uuid.uuid4().hex,
        }
        if reply_to:
            msg["replyTo"] = reply_to
        try:
            await self.ws.send(json.dumps(msg))
            log.debug("Sent message: %s", body[:80])
            return True
        except Exception as e:
            log.error("Failed to send message: %s", e)
            return False

    async def send_typing(self, is_typing: bool = True):
        """Send typing indicator via WebSocket."""
        if not self.ws:
            return
        try:
            await self.ws.send(json.dumps({"type": "typing", "isTyping": is_typing}))
        except Exception:
            pass

    # --- Lifecycle ---

    def stop(self):
        """Signal the wrapper to stop gracefully."""
        if not self._running:
            return
        self._running = False
        log.info("Shutting down...")
        # Cancel the listen loop; _on_disconnected + _shutdown run in connect()
        if self.ws:
            asyncio.ensure_future(self.ws.close())

    async def _shutdown(self):
        """Clean up resources."""
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

    # --- .env permission check ---

    @staticmethod
    def warn_env_permissions(env_path: str):
        """Warn if .env file has overly permissive permissions."""
        try:
            mode = os.stat(env_path).st_mode
            if mode & (stat.S_IRGRP | stat.S_IROTH):
                log.warning(
                    "Warning: %s is readable by group/others (mode %o). "
                    "Consider: chmod 600 %s",
                    env_path, stat.S_IMODE(mode), env_path,
                )
        except OSError:
            pass
