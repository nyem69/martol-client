# martol-client

Python agent wrapper for [martol](https://github.com/nicazmi/martol) — connects an AI agent to a chat room via WebSocket + MCP HTTP.

## How It Works

```
WebSocket ──► AgentWrapper ──► LLM Provider (Anthropic / OpenAI)
(listen)          │                    │
                  │               tool calls?
                  │                    │
                  ▼                    ▼
            send message         MCP HTTP /mcp/v1
            (WebSocket)          (action_submit, action_status)
```

- **WebSocket**: real-time listen, send messages, typing indicators
- **MCP HTTP**: structured actions that go through the server's role × risk approval matrix

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
```

## Usage

```bash
# With CLI flags
python -m martol_agent \
  --url wss://martol.plitix.com/api/rooms/<roomId>/ws \
  --api-key <martol-api-key> \
  --provider anthropic \
  --ai-key <anthropic-key> \
  --label claude:backend

# Or with env vars (from .env)
export MARTOL_WS_URL=wss://martol.plitix.com/api/rooms/<roomId>/ws
export MARTOL_API_KEY=<martol-api-key>
export AI_PROVIDER=anthropic
export AI_API_KEY=<anthropic-key>
export AGENT_LABEL=claude:backend
python -m martol_agent
```

## Providers

| Provider | Flag | Models |
|---|---|---|
| Anthropic | `--provider anthropic` | `claude-sonnet-4-20250514` (default) |
| OpenAI | `--provider openai` | `gpt-4o` (default) |
| OpenAI-compatible | `--provider openai --ai-base-url <url>` | Ollama, Groq, Together, vLLM, etc. |

## Options

| Flag | Env Var | Default | Description |
|---|---|---|---|
| `--url` | `MARTOL_WS_URL` | — | WebSocket URL (required) |
| `--api-key` | `MARTOL_API_KEY` | — | Martol agent API key (required) |
| `--ai-key` | `AI_API_KEY` | — | LLM provider API key (required) |
| `--provider` | `AI_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `--model` | `AI_MODEL` | Provider default | Model ID override |
| `--ai-base-url` | `AI_BASE_URL` | — | OpenAI-compatible base URL |
| `--mcp-url` | `MARTOL_MCP_URL` | Derived from WS URL | MCP HTTP endpoint base |
| `--label` | `AGENT_LABEL` | `agent` | Agent label for @mention detection |
| `--context` | `CONTEXT_MESSAGES` | `50` | Rolling context window size |
| `--respond` | `RESPOND_MODE` | `mention` | `mention` (only @mentions) or `all` |

## Behavior

- **Startup**: calls `chat_who` (room info) + `chat_resync` (seed context without responding to old messages)
- **Mention mode**: responds when body contains `@<label>` or `@<agent_name>` (case-insensitive)
- **All mode**: responds to every non-own message
- **Tool loop**: LLM can call `action_submit` / `action_status` via MCP HTTP, results fed back for up to 5 iterations
- **Reconnect**: exponential backoff, up to 20 attempts, stops on API key revocation (4001)

## Project Structure

```
martol_agent/
├── __init__.py
├── __main__.py              # python -m martol_agent
├── wrapper.py               # AgentWrapper (WS + MCP + LLM orchestration)
├── tools.py                 # Canonical tool definitions
└── providers/
    ├── __init__.py           # LLMProvider ABC + factory
    ├── anthropic.py          # Anthropic Claude
    └── openai_compat.py      # OpenAI / compatible APIs
```

## License

MIT
