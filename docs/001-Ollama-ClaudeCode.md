# 001: Claude Code Integration via Martol Chat

**Status:** Implemented (2026-03-02) — all core components merged to `main`.

## Problem

The current martol-client connects to an LLM API (Anthropic/OpenAI) and relays chat messages. This works for conversation, but the LLM has no access to the local filesystem — it can't read code, edit files, run commands, or do anything a developer would do at a terminal.

Claude Code is Anthropic's agentic coding tool that can read, modify, and execute code in a working directory. By bridging martol chat rooms to a Claude Code subprocess, room members could collaboratively direct an AI coding agent against a real project — with all actions gated through martol's role x risk approval matrix.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Session model | Persistent | One long-running Claude Code session per room connection. Accumulates context across messages like a user at a terminal. |
| Architecture | Separate wrapper mode | A new `ClaudeCodeWrapper` class, not a provider. Claude Code handles its own LLM calls and tool use internally — fundamentally different from the provider pattern. |
| Approval gating | Claude Code's permission system | Hook into Claude Code's built-in permission prompts. When it asks for permission, relay to the chat room and pipe back the answer. |
| Working directory | Current working directory | Operator `cd`s to the target project, then runs `python -m martol_agent --mode claude-code`. |

## Architecture: Claude Agent SDK Bridge

> **Implementation note:** The original design proposed a raw subprocess with stream-JSON I/O on stdin/stdout. During implementation, we switched to the `claude-agent-sdk` Python SDK (`ClaudeSDKClient`), which provides a higher-level, more stable API. The SDK handles stream-JSON plumbing internally and exposes a `can_use_tool` callback for permission interception — much cleaner than parsing subprocess output. The architecture diagram below reflects the implemented approach.

```
martol chat room
      |
      | WebSocket (listen/send)
      v
ClaudeCodeWrapper
      |
      | claude-agent-sdk (ClaudeSDKClient)
      | .query() → sends prompts
      | .receive_response() → collects responses
      | can_use_tool callback → permission relay
      v
Claude Code (managed by SDK)
      |
      | operates on project files in cwd
      v
local filesystem
```

### Data Flow

1. **Chat message arrives** via WebSocket
2. `ClaudeCodeWrapper` checks `_should_respond()` (same mention/all logic)
3. Message is sent to Claude Code via `client.query(prompt)`
4. Claude Code processes the prompt, potentially using tools (Read, Edit, Bash, etc.)
5. **Text responses** are collected via `client.receive_response()` and sent back to the chat room
6. **Permission prompts** trigger the `can_use_tool` callback, which relays to the chat room via `action_submit` through MCP HTTP
7. The callback returns `PermissionResultAllow` or `PermissionResultDeny` based on approval polling
8. Claude Code continues or aborts based on the result

### SDK API

The `claude-agent-sdk` provides `ClaudeSDKClient` for persistent sessions:

```python
client = ClaudeSDKClient(options=ClaudeAgentOptions(
    system_prompt={"type": "preset", "preset": "claude_code", "append": "..."},
    permission_mode="default",
    can_use_tool=handle_permission,  # async callback
    cwd=os.getcwd(),
))
await client.connect()
await client.query("read the README and summarize it")
async for message in client.receive_response():
    # AssistantMessage with TextBlock content
    # ResultMessage with success/error
```

### Permission Relay Flow

```
Claude Code wants to run: Bash("npm test")
      |
      v
SDK calls can_use_tool(tool_name, input_data, context)
      |
      v
_handle_permission posts to chat: "Permission request: Run command: `npm test`"
Also submits via MCP HTTP: action_submit(action_type="code_modify", ...)
      |
      v
_wait_for_approval polls action_status every 3s (up to 5 min)
      |
      v
Room member approves/denies via martol UI
      |
      v
Returns PermissionResultAllow or PermissionResultDeny
      |
      v
Claude Code proceeds or skips the tool
```

## New Components

### `ClaudeCodeWrapper` (new file: `martol_agent/claude_code_wrapper.py`)

Responsibilities:
- Manage Claude Code SDK session lifecycle (connect, disconnect, restart)
- Bridge WebSocket messages to/from Claude Code via `ClaudeSDKClient`
- Relay tool permission requests to the chat room via `can_use_tool` callback + `action_submit`
- Handle graceful shutdown (farewell message, disconnect SDK, close WebSocket)

Key differences from `AgentWrapper`:
- No `LLMProvider` — Claude Code handles its own LLM calls
- No `TOOLS` — Claude Code has its own built-in tools
- No tool loop — Claude Code manages tool iteration internally
- SDK session management instead of HTTP API calls

### CLI Changes (`wrapper.py` or `__main__.py`)

New flag: `--mode claude-code` (or `--provider claude-code` reusing existing flag)

```bash
# Current mode (LLM API provider)
python -m martol_agent

# Claude Code mode
python -m martol_agent --mode claude-code
```

### Configuration

New `.env` variables for Claude Code mode:

```env
# Claude Code mode settings
CLAUDE_CODE_PATH=claude              # Path to claude binary (default: "claude")
CLAUDE_CODE_MODEL=                   # Model override for Claude Code
CLAUDE_CODE_PERMISSION_MODE=default  # Permission mode (default prompts for approval)
CLAUDE_CODE_ALLOWED_TOOLS=           # Restrict available tools (optional)
```

## Session Management

### Starting

A `ClaudeSDKClient` is created with the project's working directory and a `can_use_tool` callback:

```python
options = ClaudeAgentOptions(
    system_prompt={"type": "preset", "preset": "claude_code", "append": system_append},
    permission_mode="default",
    can_use_tool=self._handle_permission,
    cwd=os.getcwd(),
)
client = ClaudeSDKClient(options=options)
await client.connect()
```

### Lifecycle

- **Start**: SDK session created after first WebSocket connection
- **Reconnect**: On WebSocket disconnect, SDK session is stopped and recreated on reconnect
- **Shutdown**: Send farewell message, disconnect SDK session, close WebSocket
- **Error recovery**: If SDK errors, logged with `exc_info=True` and swallowed

### Backpressure

- One message at a time: `asyncio.Lock()` serializes prompts to Claude Code
- Typing indicator while Claude Code is processing
- Long responses chunked at 4000 characters

## Alternatives Considered

### B: Session-ID Resumption with One-Shot Calls

Each chat message triggers `claude -p --resume <session-id> "prompt"`. Session persistence gives continuity between calls.

**Rejected because:** Not truly persistent — each call spawns a new process. Permission interception is harder in one-shot mode since the process exits before approval can be relayed.

### C: PTY-Based Terminal Emulation

Spawn Claude Code in a pseudo-terminal, parse terminal output, detect permission prompts by pattern matching.

**Rejected because:** Fragile — relies on parsing ANSI escape codes and terminal formatting. No structured protocol. Hard to maintain across Claude Code versions.

## Implementation Status

| Component | Status | Commit |
|-----------|--------|--------|
| `claude-agent-sdk` dependency | Done | `474f0ef` |
| `ClaudeCodeWrapper` class | Done | `d376396` |
| `--mode claude-code` CLI | Done | `c805365` |
| `.env.example` settings | Done | `c59c8e7` |
| README + CLAUDE.md docs | Done | `3eb3899` |
| Code review fixes | Done | `0813232` |
| Smoke test | Pending — requires live martol room |

### Known Follow-ups

- Extract shared infrastructure into a base class (`AgentWrapper` and `ClaudeCodeWrapper` share ~250 lines)
- Add `max_turns` / `max_budget_usd` safety limits
- Add `!cancel` / `!stop` chat command to interrupt long-running Claude Code operations

## Resolved Questions

1. ~~**Stream-JSON schema**~~ — Resolved by using the `claude-agent-sdk` Python SDK instead of raw stream-JSON. The SDK handles protocol details internally.
2. ~~**Permission response format**~~ — Resolved via the `can_use_tool` callback which returns `PermissionResultAllow`/`PermissionResultDeny`.
3. **Multiple users** — Current implementation: any authorized member can approve via the martol UI. The `_wait_for_approval` method polls `action_status` and accepts the first approval/denial.
4. **Long-running operations** — Handled by `asyncio.Lock()` serializing prompts. Typing indicator stays active during processing. No timeout on Claude Code operations (they may legitimately take minutes).
5. **Output chunking** — Implemented: responses are chunked at 4000 characters with only the first chunk replying to the original message.
