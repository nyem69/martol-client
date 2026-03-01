# CLAUDE.md

Guide for AI assistants working on the martol-client codebase.

## Project Overview

martol-client is a Python agent wrapper that connects AI language models to [Martol](https://github.com/nicazmi/martol) chat rooms. It uses a dual-channel architecture:

- **WebSocket** — real-time message listening, sending, and typing indicators
- **MCP HTTP** (`/mcp/v1`) — structured actions that go through a server-side role × risk approval matrix

## Project Structure

```
martol_agent/
├── __init__.py              # Empty package marker
├── __main__.py              # Entry point: python -m martol_agent
├── wrapper.py               # AgentWrapper — core orchestrator (WS + MCP + LLM)
├── tools.py                 # Provider-agnostic tool definitions + converters
└── providers/
    ├── __init__.py           # LLMProvider ABC, ToolCall/LLMResponse dataclasses, factory
    ├── anthropic.py          # Anthropic Claude implementation
    └── openai_compat.py      # OpenAI / compatible APIs (Ollama, Groq, Together, vLLM)
```

**~1,050 lines of Python total.** The entire agent lives in a single flat package.

## Setup and Running

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys
python -m martol_agent
```

Configuration is via CLI flags or environment variables (CLI takes precedence). See `.env.example` for all options.

## Dependencies

| Package | Purpose |
|---------|---------|
| `websockets>=12.0,<14.0` | WebSocket client |
| `anthropic>=0.40.0` | Anthropic Claude SDK |
| `openai>=1.50.0` | OpenAI SDK (also powers compatible APIs) |
| `aiohttp>=3.9.0` | Async HTTP for MCP calls |
| `python-dotenv>=1.0.0` | Loads `.env` file into `os.environ` at startup |

No build system, no bundler — runs directly as a Python module.

## Architecture

```
CLI args / env vars
       │
  python -m martol_agent
       │
  AgentWrapper (wrapper.py)
  ├── WebSocket channel — listen/send/typing
  ├── MCP HTTP channel  — action_submit, action_status, chat_read, chat_resync
  └── LLMProvider       — strategy pattern for AI backends
      ├── AnthropicProvider  (default model: claude-sonnet-4-20250514)
      └── OpenAICompatProvider (default model: gpt-4o)
```

### Key flows

1. **Startup**: `_startup_sync()` calls `chat_who` which returns `self_user_id` — the agent resolves its own identity and display name from the member list. Then `chat_resync` seeds context, followed by an AI disclosure message.
2. **Message handling**: WebSocket messages arrive in `_listen()` → `_handle_message()` → `_should_respond()` gates on mention/all mode → `_generate_response()` calls the LLM.
3. **Tool loop**: LLM can return tool calls (`action_submit`/`action_status`), executed via MCP HTTP. Results are fed back for up to `MAX_TOOL_ITERATIONS` (5) rounds.
4. **Reconnection**: Exponential backoff (1s → 30s max), up to 20 attempts. Stops permanently on code 4001 (API key revoked).

### State management

All state lives as instance variables on `AgentWrapper`:
- `self.agent_user_id` / `self.agent_name` — resolved from `chat_who` at startup (server-authoritative)
- `self.conversation` — rolling context window (default 50 messages)
- `self.last_known_id` — sequence tracking for reconnection
- `self._responding` — `asyncio.Lock()` serializes LLM calls

## Code Conventions

### Naming
- **Classes**: PascalCase (`AgentWrapper`, `AnthropicProvider`, `LLMResponse`)
- **Functions/methods**: snake_case (`derive_mcp_url`, `_build_system_prompt`)
- **Private methods**: leading underscore (`_listen`, `_handle_message`)
- **Constants**: UPPER_SNAKE_CASE (`MAX_RECONNECT_DELAY`, `BASE_RECONNECT_DELAY`)

### Patterns
- **Async throughout** — all I/O uses `async`/`await` with `asyncio`
- **Strategy pattern** — `LLMProvider` ABC with factory function `create_provider()`
- **Dataclasses** — `ToolCall` and `LLMResponse` for structured data
- **Lazy imports** — provider modules imported inside `create_provider()` and `_build_tool_result_messages()`
- **Canonical tool schema** — tools defined once in `tools.py`, converted per-provider via `to_anthropic_tools()` / `to_openai_tools()`

### Logging
- Uses `logging` module with logger name `"martol-agent"`
- Format: `%(asctime)s [%(levelname)s] %(message)s` with `%H:%M:%S` timestamps
- Log level: INFO by default

### Error handling
- Import failures exit with user-friendly messages (`sys.exit(1)`)
- WebSocket errors trigger reconnection with backoff
- MCP HTTP calls have 30-second timeouts and return `None` on failure
- LLM call failures are logged with `exc_info=True` and swallowed (no crash)

## Tools (MCP)

Two tools are exposed to the LLM:

| Tool | Purpose |
|------|---------|
| `action_submit` | Submit structured actions (code changes, deploys, config) for human approval |
| `action_status` | Check approval status of a previously submitted action |

Action types: `question_answer`, `code_review`, `code_write`, `code_modify`, `code_delete`, `deploy`, `config_change`

Risk levels: `low`, `medium`, `high` (server may override)

## Adding a New LLM Provider

1. Create `martol_agent/providers/<name>.py`
2. Implement `LLMProvider` ABC (the `chat()` method)
3. Add static methods `format_tool_result()` and `format_assistant_message()`
4. Register in `create_provider()` factory in `providers/__init__.py`
5. Add the choice to the `--provider` argparse in `wrapper.py`
6. Handle the new provider in `_build_tool_result_messages()` in `wrapper.py`

## Testing

No test suite exists yet. No linters or formatters are configured.

## Important Notes

- **No `.env` in git** — `.env` is gitignored; use `.env.example` as a template
- **Never commit secrets** — API keys belong in `.env` or environment variables only
- **Python 3.10+** — the codebase uses `X | Y` union syntax (PEP 604)
- **Single-package layout** — everything is under `martol_agent/`, no `src/` directory
- **MIT licensed**
