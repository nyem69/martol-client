# 001: Claude Code Integration via Martol Chat

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

## Architecture: Stream-JSON Subprocess Bridge

```
martol chat room
      |
      | WebSocket (listen/send)
      v
ClaudeCodeWrapper
      |
      | stdin: stream-json (user prompts from chat)
      | stdout: stream-json (responses, tool use, permission prompts)
      v
claude subprocess (--input-format stream-json --output-format stream-json)
      |
      | operates on project files in cwd
      v
local filesystem
```

### Data Flow

1. **Chat message arrives** via WebSocket
2. `ClaudeCodeWrapper` checks `_should_respond()` (same mention/all logic)
3. Message is written to Claude Code's stdin as a stream-json prompt
4. Claude Code processes the prompt, potentially using tools (Read, Edit, Bash, etc.)
5. **Text responses** from Claude Code's stdout are sent back to the chat room
6. **Permission prompts** (Claude Code asking to run a command or edit a file) are relayed to the chat room via `action_submit` through MCP HTTP
7. Approval/denial is piped back to Claude Code's stdin
8. Claude Code continues or aborts based on the answer

### Stream-JSON Protocol

Claude Code's `--input-format stream-json` accepts newline-delimited JSON on stdin:

```json
{"type": "user", "content": "read the README and summarize it"}
```

Claude Code's `--output-format stream-json` emits newline-delimited JSON on stdout:

```json
{"type": "assistant", "subtype": "text", "text": "Here's a summary..."}
{"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}
{"type": "permission_request", "tool": "Bash", "input": {"command": "npm test"}}
{"type": "result", "subtype": "text", "text": "All 42 tests passed."}
```

> Note: Exact stream-json schema needs verification against Claude Code's actual output. The above is illustrative.

### Permission Relay Flow

```
Claude Code wants to run: Bash("npm test")
      |
      v
ClaudeCodeWrapper detects permission_request in stream-json
      |
      v
Posts to chat room: "Claude Code wants to run: `npm test` — approve?"
Also submits via MCP HTTP: action_submit(action_type="code_write", ...)
      |
      v
Room member approves/denies
      |
      v
ClaudeCodeWrapper writes approval/denial to Claude Code's stdin
      |
      v
Claude Code proceeds or skips the tool
```

## New Components

### `ClaudeCodeWrapper` (new file: `martol_agent/claude_code_wrapper.py`)

Responsibilities:
- Manage Claude Code subprocess lifecycle (spawn, monitor, restart)
- Bridge WebSocket messages to/from Claude Code's stream-json stdin/stdout
- Parse stream-json output to extract text responses and permission prompts
- Relay permission prompts to the chat room via action_submit
- Handle graceful shutdown (kill subprocess on Ctrl+C)

Key differences from `AgentWrapper`:
- No `LLMProvider` — Claude Code handles its own LLM calls
- No `TOOLS` — Claude Code has its own built-in tools
- No tool loop — Claude Code manages tool iteration internally
- Subprocess lifecycle management instead of HTTP API calls

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

## Subprocess Management

### Spawning

Claude Code is spawned as an async subprocess with stream-json I/O:

```python
proc = await asyncio.create_subprocess_exec(
    "claude",
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--permission-mode", "default",
    "--system-prompt", system_prompt,
    stdin=asyncio.subprocess.PIPE,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
```

The `CLAUDECODE` environment variable must be unset to avoid the nesting check when running from within a Claude Code session during development.

### Lifecycle

- **Start**: Spawned on first WebSocket connection
- **Monitor**: Watch for subprocess exit, restart if unexpected
- **Shutdown**: Kill subprocess on Ctrl+C / SIGTERM, send farewell to room
- **Crash recovery**: If Claude Code crashes, log the error and restart with a new session

### Backpressure

- One message at a time: queue incoming chat messages, feed to Claude Code sequentially
- Typing indicator while Claude Code is processing
- Timeout: if Claude Code doesn't respond within N seconds, log warning (but don't kill — it may be doing real work)

## Alternatives Considered

### B: Session-ID Resumption with One-Shot Calls

Each chat message triggers `claude -p --resume <session-id> "prompt"`. Session persistence gives continuity between calls.

**Rejected because:** Not truly persistent — each call spawns a new process. Permission interception is harder in one-shot mode since the process exits before approval can be relayed.

### C: PTY-Based Terminal Emulation

Spawn Claude Code in a pseudo-terminal, parse terminal output, detect permission prompts by pattern matching.

**Rejected because:** Fragile — relies on parsing ANSI escape codes and terminal formatting. No structured protocol. Hard to maintain across Claude Code versions.

## Open Questions

1. **Stream-JSON schema**: The exact format of permission prompts in stream-json output needs to be verified by testing outside a Claude Code session.
2. **Permission response format**: How to pipe approval/denial back via stream-json stdin — need to verify the protocol.
3. **Multiple users**: When a permission prompt is posted to the room, any authorized member can approve. Need to decide if we wait for the first response or require a specific role.
4. **Long-running operations**: Claude Code can take minutes for complex tasks. How to handle WebSocket keepalive and typing indicators during extended processing.
5. **Output chunking**: Claude Code may produce very long outputs. Should we chunk them into multiple chat messages or truncate?
