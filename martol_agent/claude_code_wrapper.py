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

        # Deny-list for sensitive file paths
        deny_patterns_str = os.environ.get("CLAUDE_CODE_DENY_PATHS", ".env*,*.key,*.pem,*.p12")
        self.deny_path_patterns = [p.strip() for p in deny_patterns_str.split(",") if p.strip()]

        # Approval polling timeout (seconds)
        self.approval_timeout = int(os.environ.get("CLAUDE_CODE_APPROVAL_TIMEOUT", "60"))

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
            f"Responses should not be relied upon without verification. "
            f"You can opt out of having your messages included in AI context via your room settings."
        )

    # ── Trigger ───────────────────────────────────────────────────────

    async def _on_trigger(self, payload: dict):
        """Handle a message that should trigger a Claude Code response."""
        if self._responding.locked():
            log.info("Already generating response, skipping new trigger")
            return
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

        # Inject project brief
        if self.room_brief:
            system_append += "\nPROJECT BRIEF:\n"
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
                            system_append += f"## {heading}\n{val}\n\n"
                else:
                    system_append += self.room_brief
            except (ValueError, TypeError):
                system_append += self.room_brief

        # Instruct how to update the brief
        system_append += (
            "\nBRIEF UPDATE CAPABILITY:\n"
            "When asked to fill or update the project brief, analyze the codebase "
            "and output a fenced JSON block tagged `brief_update` with the sections "
            "you want to set. The wrapper will call the server API automatically.\n"
            "Format:\n"
            "```brief_update\n"
            '{"goal": "...", "stack": "...", "conventions": "...", "phase": "...", "notes": "..."}\n'
            "```\n"
            "Only include sections you want to update. Omitted sections stay unchanged.\n"
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
        """Stop the Claude Code SDK session gracefully."""
        if self.claude_client:
            client = self.claude_client
            self.claude_client = None
            try:
                await client.disconnect()
            except (asyncio.CancelledError, Exception):
                pass
            log.info("Claude Code session stopped")

    # ── Lifecycle hooks ─────────────────────────────────────────────

    def stop(self):
        """Stop Claude Code session before closing WebSocket."""
        if self.claude_client:
            asyncio.ensure_future(self._stop_claude_session())
        super().stop()

    async def _on_connected(self):
        await self._start_claude_session()

    async def _on_disconnected(self):
        await self._stop_claude_session()

    # ── Permission Handling ──────────────────────────────────────────

    async def _handle_permission(
        self,
        tool_name: str,
        input_data: dict,
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Relay permission requests to the chat room via action_submit."""
        import fnmatch

        # Check deny-list paths
        paths_to_check = []
        if isinstance(input_data, dict):
            for key in ("file_path", "path", "directory"):
                if key in input_data:
                    paths_to_check.append(str(input_data[key]))

        for path in paths_to_check:
            basename = os.path.basename(path)
            for pattern in self.deny_path_patterns:
                if fnmatch.fnmatch(basename, pattern) or fnmatch.fnmatch(path, pattern):
                    log.warning("Path denied by CLAUDE_CODE_DENY_PATHS: %s", path)
                    return PermissionResultDeny(message=f"Access to '{basename}' is restricted")

        # Block WebFetch to private/internal IP ranges (SSRF protection)
        if tool_name == "WebFetch":
            from urllib.parse import urlparse
            import ipaddress
            url = input_data.get("url", "")
            hostname = urlparse(url).hostname or ""
            try:
                ip = ipaddress.ip_address(hostname)
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    return PermissionResultDeny(message=f"WebFetch blocked: {hostname} is a private/internal IP")
            except ValueError:
                pass  # hostname is a domain name, not an IP — allow

        # Hard-deny tools not in whitelist (if whitelist is configured)
        if self.claude_allowed_tools and not self._tool_allowed(tool_name):
            log.warning("Tool %s blocked by whitelist", tool_name)
            return PermissionResultDeny(message=f"Tool '{tool_name}' not in allowed list")

        # Format the tool call for human review (sanitized for chat broadcast)
        if tool_name in ("Write", "Edit"):
            description = f"Write/edit file: {input_data.get('file_path', 'unknown')}"
        elif tool_name == "Bash":
            cmd = str(input_data.get("command", ""))
            description = f"Run command: `{cmd[:100]}`" + ("..." if len(cmd) > 100 else "")
        else:
            description = f"Use tool: {tool_name}"

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
        # Use dbId if available (ME-12), fall back to serverSeqId
        trigger_seq = str(self.last_known_id)
        trigger_id = self._id_map.get(trigger_seq, trigger_seq)
        result = await self._mcp_call("action_submit", {
            "action_type": (
                "code_write" if tool_name in ("Write", "Edit", "NotebookEdit") else
                "code_review" if tool_name in ("Read", "Grep", "Glob", "LS") else
                "code_modify"
            ),
            "risk_level": risk,
            "trigger_message_id": trigger_id,
            "description": description,
            "payload": {"tool": tool_name, "input": input_data},
        })

        if result and result.get("ok"):
            action_id = result.get("data", {}).get("action_id")
            if action_id:
                # Poll for approval status
                status = await self._wait_for_approval(action_id)
                if status == "approved":
                    await self.send_message(f"Approved. Proceeding with: {description}")
                    return PermissionResultAllow(updated_input=input_data)
                elif status is None:
                    await self.send_message(f"Approval timed out. Skipping: {description}")
                    return PermissionResultDeny(message="Approval timed out")
                else:
                    await self.send_message(f"Denied. Skipping: {description}")
                    return PermissionResultDeny(message=f"Action {status} by room member")

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

    async def _wait_for_approval(self, action_id: str) -> str | None:
        """Poll for action approval with periodic feedback."""
        timeout = self.approval_timeout
        elapsed = 0
        interval = 3
        last_feedback = 0

        while elapsed < timeout:
            result = await self._mcp_call("action_status", {"action_id": action_id})
            if result and result.get("ok"):
                status = result.get("data", {}).get("status")
                if status in ("approved", "rejected", "expired"):
                    return status

            elapsed += interval
            # Send periodic feedback every 15 seconds
            if elapsed - last_feedback >= 15:
                await self.send_typing(True)
                last_feedback = elapsed

            await asyncio.sleep(interval)

        return None  # Timeout

    # ── Brief Update Interception ────────────────────────────────────

    async def _handle_brief_update_blocks(self, text: str) -> str:
        """Scan response for ```brief_update blocks, call MCP, replace with status."""
        import re
        pattern = re.compile(r"```brief_update\s*\n(.*?)\n```", re.DOTALL)

        matches = list(pattern.finditer(text))
        if not matches:
            return text

        for match in reversed(matches):  # reverse to preserve offsets
            try:
                params = json.loads(match.group(1))
                if not isinstance(params, dict):
                    continue

                # Filter to valid keys only
                valid_keys = {"goal", "stack", "conventions", "phase", "notes"}
                filtered = {k: v for k, v in params.items() if k in valid_keys and isinstance(v, str)}
                if not filtered:
                    continue

                result = await self._mcp_call("brief_update", filtered)
                if result and result.get("ok"):
                    new_ver = result.get("data", {}).get("version", "?")
                    replacement = f"**Brief updated** (v{new_ver})"
                    # Update local state
                    self.room_brief_version = int(new_ver) if isinstance(new_ver, int) else 0
                    brief = await self._mcp_call("brief_get_active", {})
                    if brief and brief.get("ok"):
                        self.room_brief = brief.get("data", {}).get("brief")
                    log.info("Brief updated to v%s via Claude Code", new_ver)
                else:
                    err = result.get("error", "unknown error") if result else "MCP call failed"
                    replacement = f"**Brief update failed:** {err}"
                    log.warning("brief_update failed: %s", err)

                text = text[:match.start()] + replacement + text[match.end():]
            except (json.JSONDecodeError, Exception) as e:
                log.warning("Failed to parse brief_update block: %s", e)

        return text

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

                    # Intercept brief_update blocks and call MCP
                    full_text = await self._handle_brief_update_blocks(full_text)

                    # Chunk long responses (martol may have message size limits)
                    max_len = 4000
                    reply_to = trigger.get("serverSeqId") or trigger.get("id")
                    for i in range(0, len(full_text), max_len):
                        chunk = full_text[i:i + max_len]
                        success = await self.send_message(chunk, reply_to=reply_to if i == 0 else None)
                        if not success:
                            log.warning("Failed to send chunk %d, aborting", i // max_len)
                            break
                        if i + max_len < len(full_text):
                            await asyncio.sleep(0.15)

            except Exception as e:
                if not self._running:
                    return  # Expected during shutdown
                log.error("Failed to process with Claude Code: %s", e)
                log.debug("Full traceback:", exc_info=True)
            finally:
                await self.send_typing(False)
