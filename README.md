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

The wrapper connects to a martol room using an API key created by the room owner. It listens for @mentions, sends messages to an LLM, and relays responses back. Structured actions (code changes, deploys) go through MCP HTTP and the server's role × risk approval matrix.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
python -m martol_agent
```

### Setup steps

1. **Get an API key** — a room owner or lead creates an agent in the martol web UI and copies the API key
2. **Get the room URL** — the WebSocket URL for the room (e.g. `wss://martol.plitix.com/api/rooms/<roomId>/ws`)
3. **Configure** — set `MARTOL_API_KEY`, `MARTOL_WS_URL`, and `AI_API_KEY` in `.env`
4. **Run** — `python -m martol_agent`

The agent's name and identity are resolved automatically from the server via the API key — no manual configuration needed.

## Usage

Configure via `.env` (recommended) or CLI flags (takes precedence):

```bash
python -m martol_agent \
  --url wss://martol.plitix.com/api/rooms/<roomId>/ws \
  --api-key <martol-api-key> \
  --provider anthropic \
  --ai-key <anthropic-key>
```

## Use Cases

### DevOps Assistant
```
You:    @claude:backend deploy the latest tag to staging
Agent:  I'll submit a deploy action for the latest tag to staging.
        → action_submit(action_type="deploy", risk_level="medium", ...)
        ✓ Awaiting approval from a maintainer.
```

### Code Review Bot
```
You:    @claude:backend review the auth changes in PR #42
Agent:  I'll review the changes and submit my findings.
        → action_submit(action_type="code_review", risk_level="low", ...)
```

### Engineering Support
```
You:    @claude:backend how does our rate limiter handle burst traffic?
Agent:  Based on the discussion above, the rate limiter uses a token bucket
        algorithm with a burst capacity of 100 requests...
```

### Multi-Step Workflows
The tool loop (up to 5 iterations) lets the agent submit an action, poll its approval status, and follow up.

```
You:    @claude:backend write a migration to add an index on users.email, then deploy it
Agent:  → action_submit(action_type="code_write", ...)
        → action_status(action_id=17)  — approved
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

## Providers

| Provider | Flag | Default Model |
|---|---|---|
| Anthropic | `--provider anthropic` | `claude-sonnet-4-20250514` |
| OpenAI | `--provider openai` | `gpt-4o` |
| OpenAI-compatible | `--provider openai --ai-base-url <url>` | Ollama, Groq, Together, vLLM, etc. |

## Multiple Agents

Use named profiles to run multiple agents from one machine. Each profile is a separate `.env` file:

```bash
# Create profile files
cp .env.example .env.claude    # fill in Claude agent keys
cp .env.example .env.gpt       # fill in GPT agent keys

# Run each in a separate terminal
python -m martol_agent --profile claude
python -m martol_agent --profile gpt
```

Each agent gets its own API key (created in the martol web UI), its own LLM provider config, and connects as a distinct agent in the room. The default `.env` is used when no `--profile` is specified.

## Options

| Flag | Env Var | Default | Description |
|---|---|---|---|
| `--profile` | — | — | Named profile (loads `.env.<profile>`) |
| `--url` | `MARTOL_WS_URL` | — | WebSocket URL (required) |
| `--api-key` | `MARTOL_API_KEY` | — | Martol agent API key (required) |
| `--ai-key` | `AI_API_KEY` | — | LLM provider API key (required) |
| `--provider` | `AI_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `--model` | `AI_MODEL` | Provider default | Model ID override |
| `--ai-base-url` | `AI_BASE_URL` | — | OpenAI-compatible base URL |
| `--mcp-url` | `MARTOL_MCP_URL` | Derived from WS URL | MCP HTTP endpoint base |
| `--context` | `CONTEXT_MESSAGES` | `50` | Rolling context window size |
| `--respond` | `RESPOND_MODE` | `mention` | `mention` (only @mentions) or `all` |

## Behavior

- **Startup** — calls `chat_who` to resolve identity and room info, then `chat_resync` to seed context, then announces presence with AI disclosure
- **Mention mode** — responds when message contains `@<agent_name>` (case-insensitive, name resolved from server)
- **All mode** — responds to every non-own message
- **Tool loop** — LLM can call `action_submit` / `action_status` via MCP HTTP, results fed back for up to 5 iterations
- **Reconnect** — exponential backoff (1s → 30s), up to 20 attempts. Stops permanently on API key revocation (4001)

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

## Requirements

- Python 3.10+
- `websockets`, `anthropic`, `openai`, `aiohttp`, `python-dotenv`

## License

MIT
