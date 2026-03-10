#!/usr/bin/env python3
"""
Codex Bridge — connects an OpenAI Codex session to a Martol chat room.

Runs `codex mcp-server` as a subprocess and communicates via JSON-RPC
over stdio. Chat messages become prompts, Codex responses become chat
messages. Tool calls (file reads/writes, shell commands) are handled
by Codex itself within its sandbox.
"""

import asyncio
import json
import logging
import os
import shutil

from martol_agent.base_wrapper import BaseWrapper

log = logging.getLogger("martol-agent")


class CodexWrapper(BaseWrapper):
    """Bridges a Martol chat room to a persistent Codex MCP server session."""

    def __init__(
        self,
        ws_url: str,
        api_key: str,
        mcp_url: str | None = None,
        context_size: int = 50,
        respond_mode: str = "mention",
        codex_model: str | None = None,
        codex_sandbox: str = "read-only",
        codex_approval_policy: str = "on-failure",
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
        self.codex_model = codex_model
        self.codex_sandbox = codex_sandbox
        self.codex_approval_policy = codex_approval_policy
        self.member_count: int = 0

        # Codex MCP server subprocess
        self._codex_proc: asyncio.subprocess.Process | None = None
        self._rpc_id: int = 0
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

        # Codex thread ID for conversation continuity
        self._thread_id: str | None = None

    # ── Disclosure ────────────────────────────────────────────────────

    async def _send_disclosure(self):
        """Send AI disclosure message for Codex mode."""
        display = self.agent_name or "agent"
        model_display = self.codex_model or "default"
        await self.send_message(
            f"[AI Agent] {display} connected (powered by OpenAI Codex, model: {model_display}). "
            f"I am an AI assistant with access to this project's codebase. "
            f"Responses should not be relied upon without verification. "
            f"You can opt out of having your messages included in AI context via your room settings."
        )

    # ── Trigger ───────────────────────────────────────────────────────

    async def _on_trigger(self, payload: dict):
        """Handle a message that should trigger a Codex response."""
        if self._responding.locked():
            log.info("Already generating response, skipping new trigger")
            return
        asyncio.create_task(self._send_to_codex(payload))

    # ── Codex MCP Server Lifecycle ────────────────────────────────────

    async def _start_codex_server(self) -> None:
        """Start the Codex MCP server subprocess."""
        codex_bin = shutil.which("codex")
        if not codex_bin:
            log.error("codex CLI not found in PATH. Install: npm install -g @openai/codex")
            return

        self._codex_proc = await asyncio.create_subprocess_exec(
            codex_bin, "mcp-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(),
        )
        log.info("Codex MCP server started (pid=%d)", self._codex_proc.pid)

        # Initialize MCP session
        init_result = await self._rpc_call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "martol-agent", "version": "1.0"},
        })
        if init_result:
            server_info = init_result.get("serverInfo", {})
            log.info("Codex MCP server: %s v%s",
                     server_info.get("name", "unknown"),
                     server_info.get("version", "unknown"))

        # Send initialized notification
        await self._rpc_notify("notifications/initialized", {})

        # Start stderr reader
        self._reader_task = asyncio.create_task(self._read_stderr())

        log.info("Codex session ready")

    async def _stop_codex_server(self) -> None:
        """Stop the Codex MCP server subprocess."""
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None

        if self._codex_proc:
            proc = self._codex_proc
            self._codex_proc = None
            try:
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                proc.kill()
            log.info("Codex MCP server stopped")

        self._thread_id = None

    async def _read_stderr(self) -> None:
        """Read stderr from Codex process for logging."""
        try:
            while self._codex_proc and self._codex_proc.stderr:
                line = await self._codex_proc.stderr.readline()
                if not line:
                    break
                log.debug("codex stderr: %s", line.decode().rstrip())
        except asyncio.CancelledError:
            pass

    # ── JSON-RPC Communication ────────────────────────────────────────

    async def _read_stdout_line(self, timeout: float) -> bytes | None:
        """Read a newline-delimited line from Codex stdout without size limit.

        The default asyncio StreamReader.readline() has a 64KB buffer limit
        which causes 'Separator is not found, and chunk exceed the limit'
        errors on large JSON-RPC responses. This reads in chunks instead.
        """
        stdout = self._codex_proc.stdout
        chunks: list[bytes] = []
        while True:
            chunk = await asyncio.wait_for(stdout.read(65536), timeout=timeout)
            if not chunk:
                return b"".join(chunks) if chunks else None
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        data = b"".join(chunks)
        # If there's data after the newline, push it back into the buffer
        idx = data.index(b"\n")
        remainder = data[idx + 1:]
        if remainder:
            stdout._buffer = remainder + bytes(stdout._buffer)  # type: ignore[attr-defined]
        return data[:idx + 1]

    async def _rpc_call(self, method: str, params: dict, timeout: float = 120) -> dict | None:
        """Send a JSON-RPC request and wait for response."""
        if not self._codex_proc or not self._codex_proc.stdin:
            log.error("Codex process not running")
            return None

        self._rpc_id += 1
        req_id = self._rpc_id
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[req_id] = future

        try:
            data = json.dumps(request) + "\n"
            self._codex_proc.stdin.write(data.encode())
            await self._codex_proc.stdin.drain()
            log.debug("RPC request #%d: %s", req_id, method)

            # Read responses until we get ours
            while not future.done():
                line = await self._read_stdout_line(timeout)
                if not line:
                    future.set_exception(ConnectionError("Codex process closed stdout"))
                    break

                try:
                    msg = json.loads(line.decode())
                except json.JSONDecodeError:
                    log.debug("Non-JSON from codex: %s", line.decode().rstrip())
                    continue

                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending_requests:
                    pending = self._pending_requests.pop(msg_id)
                    if "error" in msg:
                        log.error("RPC error #%d: %s", msg_id, msg["error"])
                        pending.set_result(None)
                    else:
                        pending.set_result(msg.get("result"))
                elif msg_id is None:
                    # Notification from server (no id)
                    log.debug("RPC notification: %s", msg.get("method", "unknown"))

            return future.result() if future.done() else None

        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            log.error("RPC timeout for %s (#%d) after %ds", method, req_id, timeout)
            return None
        except Exception as e:
            self._pending_requests.pop(req_id, None)
            log.error("RPC call %s failed: %s", method, e)
            return None

    async def _rpc_notify(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self._codex_proc or not self._codex_proc.stdin:
            return

        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        data = json.dumps(notification) + "\n"
        self._codex_proc.stdin.write(data.encode())
        await self._codex_proc.stdin.drain()

    # ── Codex Tool Calls ──────────────────────────────────────────────

    async def _call_codex_tool(self, prompt: str) -> str | None:
        """Call the codex or codex-reply MCP tool."""
        if self._thread_id:
            # Continue existing conversation
            tool_name = "codex-reply"
            arguments = {
                "threadId": self._thread_id,
                "prompt": prompt,
            }
        else:
            # Start new session
            tool_name = "codex"
            arguments: dict = {"prompt": prompt}
            if self.codex_model:
                arguments["model"] = self.codex_model
            arguments["sandbox"] = self.codex_sandbox
            arguments["approval-policy"] = self.codex_approval_policy
            arguments["cwd"] = os.getcwd()

            # Build developer instructions
            display = self.agent_name or "agent"
            instructions = (
                f"You are {display}, an AI coding assistant in a Martol chat room "
                f'called "{self.room_name or "unknown"}" with {self.member_count} members.\n'
                f"Keep responses concise. Use markdown formatting for code."
            )
            if self.room_brief:
                instructions += "\n\nPROJECT BRIEF:\n"
                try:
                    parsed = json.loads(self.room_brief)
                    if isinstance(parsed, dict) and "goal" in parsed:
                        for key, heading in [
                            ("goal", "Goal"), ("stack", "Stack"),
                            ("conventions", "Conventions"), ("phase", "Current Phase"),
                            ("notes", "Notes"),
                        ]:
                            val = parsed.get(key, "")
                            if val:
                                instructions += f"## {heading}\n{val}\n\n"
                    else:
                        instructions += self.room_brief
                except (ValueError, TypeError):
                    instructions += self.room_brief

            arguments["developer-instructions"] = instructions

        result = await self._rpc_call("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        }, timeout=180)  # Codex can be slow

        if not result:
            return None

        # Extract content from MCP tool result
        content_parts = result.get("content", [])
        text_parts = []
        for part in content_parts:
            if part.get("type") == "text":
                text = part.get("text", "")
                # Parse structured content for threadId
                try:
                    structured = json.loads(text)
                    if isinstance(structured, dict):
                        if "threadId" in structured:
                            self._thread_id = structured["threadId"]
                            log.debug("Codex thread: %s", self._thread_id)
                        if "content" in structured:
                            text_parts.append(structured["content"])
                            continue
                except (json.JSONDecodeError, TypeError):
                    pass
                text_parts.append(text)

        return "\n".join(text_parts) if text_parts else None

    # ── Main Prompt Handler ───────────────────────────────────────────

    async def _send_to_codex(self, trigger: dict) -> None:
        """Send a chat message to Codex and relay the response."""
        async with self._responding:
            if not self._codex_proc:
                log.warning("Codex process not running")
                return

            try:
                await self.send_typing(True)

                sender = trigger.get("senderName") or trigger.get("sender_name") or "unknown"
                body = trigger.get("body", "")
                prompt = f"[{sender}]: {body}"

                log.info("Sending to Codex (%d chars)", len(prompt))
                response = await self._call_codex_tool(prompt)

                if response:
                    # Chunk long responses
                    max_len = 4000
                    reply_to = trigger.get("serverSeqId") or trigger.get("id")
                    for i in range(0, len(response), max_len):
                        chunk = response[i:i + max_len]
                        success = await self.send_message(chunk, reply_to=reply_to if i == 0 else None)
                        if not success:
                            log.warning("Failed to send chunk %d, aborting", i // max_len)
                            break
                        if i + max_len < len(response):
                            await asyncio.sleep(0.15)
                else:
                    log.warning("Empty response from Codex")

            except Exception as e:
                if not self._running:
                    return
                log.error("Failed to process with Codex: %s", e)
                log.debug("Full traceback:", exc_info=True)
            finally:
                await self.send_typing(False)

    # ── Lifecycle hooks ─────────────────────────────────────────────

    async def _on_connected(self):
        await self._start_codex_server()

    async def _on_disconnected(self):
        await self._stop_codex_server()

    def stop(self):
        """Stop Codex server before closing WebSocket."""
        if self._codex_proc:
            asyncio.ensure_future(self._stop_codex_server())
        super().stop()
