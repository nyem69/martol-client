# martol-client

Python agent wrapper for [martol](https://github.com/nyem69/martol) — connects an AI agent to a chat room via WebSocket + MCP HTTP.

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

## Use Cases

### DevOps Assistant
Add an AI agent to your ops room that can propose deployments, config changes, and infrastructure actions — all gated by the server's role × risk approval matrix.

```
You:    @claude:backend deploy the latest tag to staging
Agent:  I'll submit a deploy action for the latest tag to staging.
        → action_submit(action_type="deploy", risk_level="medium", ...)
        ✓ Awaiting approval from a maintainer.
```

### Code Review Bot
The agent can review code when asked and submit structured feedback through the approval pipeline.

```
You:    @claude:backend review the auth changes in PR #42
Agent:  I'll review the changes and submit my findings.
        → action_submit(action_type="code_review", risk_level="low", ...)
```

### Engineering Support
Answer technical questions directly in chat, with full conversation context (rolling window of recent messages).

```
You:    @claude:backend how does our rate limiter handle burst traffic?
Agent:  Based on the discussion above, the rate limiter uses a token bucket
        algorithm with a burst capacity of 100 requests...
```

### Multi-Step Workflows
The tool loop (up to 5 iterations) lets the agent submit an action, poll its approval status, and follow up based on the result.

```
You:    @claude:backend write a migration to add an index on users.email, then deploy it
Agent:  I'll start by submitting the migration code for review.
        → action_submit(action_type="code_write", ...)
        → action_status(action_id=17)  — approved
        Now submitting the deploy action.
        → action_submit(action_type="deploy", ...)
```

### Self-Hosted / Private LLM
Use any OpenAI-compatible API (Ollama, vLLM, etc.) to keep all data on your own infrastructure.

```bash
python -m martol_agent \
  --provider openai \
  --ai-base-url http://localhost:11434/v1 \
  --model llama3
```

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
python -m martol_agent
```

Configure everything in `.env` — no CLI flags needed:

```env
MARTOL_WS_URL=wss://martol.plitix.com/api/rooms/<roomId>/ws
MARTOL_API_KEY=<martol-api-key>
AI_PROVIDER=anthropic
AI_API_KEY=<anthropic-key>
```

## Usage

CLI flags also work and take precedence over `.env`:

```bash
python -m martol_agent \
  --url wss://martol.plitix.com/api/rooms/<roomId>/ws \
  --api-key <martol-api-key> \
  --provider anthropic \
  --ai-key <anthropic-key> \
  --label claude:backend
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
