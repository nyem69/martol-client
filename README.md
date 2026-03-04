# martol-client

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-green)](LICENSE)
[![WebSocket](https://img.shields.io/badge/transport-WebSocket-purple)](https://github.com/nyem69/martol-client)
[![MCP](https://img.shields.io/badge/protocol-MCP-orange)](https://github.com/nyem69/martol-client)

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
3. **Configure** — set `MARTOL_API_KEY`, `MARTOL_WS_URL`, `AI_API_KEY`, and optionally `MARTOL_HMAC_SECRET` in `.env`
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

## Example Session

A room with two AI agents (`claude` on Anthropic, `qwen3` on local Ollama) and a human developer:

```
┌─ Test Room ─────────────────────────────── 3 online ─┐
│                                                       │
│  azmi    @qwen3 what does our auth middleware do?     │
│                                                       │
│  qwen3   The auth middleware in middleware/auth.js     │
│  AGENT   checks for a valid JWT in the Authorization  │
│          header. If missing or expired, it returns     │
│          401. It also attaches `req.user` with the     │
│          decoded payload for downstream handlers.      │
│                                                       │
│  azmi    ↩ qwen3: can it handle refresh tokens?       │
│                                                       │
│  qwen3   Not currently. It only validates access       │
│  AGENT   tokens. You'd need to add a /refresh          │
│          endpoint that issues new access tokens         │
│          from a valid refresh token.                    │
│                                                       │
│  azmi    @claude review the auth middleware and         │
│          suggest improvements                          │
│                                                       │
│  claude  I'll review the code and submit my findings.  │
│  AGENT   → action_submit(action_type="code_review",   │
│             risk_level="low",                          │
│             description="Auth middleware review")       │
│                                                       │
│          Found 3 issues:                               │
│          1. No refresh token rotation                  │
│          2. JWT secret is hardcoded in the file        │
│          3. Missing rate limit on failed attempts       │
│                                                       │
│  azmi    ↩ claude: fix issue #2, move the secret       │
│          to environment variables                      │
│                                                       │
│  claude  → action_submit(action_type="code_modify",   │
│  AGENT     risk_level="medium",                        │
│             description="Move JWT secret to env var")  │
│          ⏳ Awaiting approval...                       │
│                                                       │
│  azmi    ✓ approved                                    │
│                                                       │
│  claude  Done. Changed `const SECRET = "..."` to       │
│  AGENT   `const SECRET = process.env.JWT_SECRET` and   │
│          added JWT_SECRET to .env.example.             │
│                                                       │
└───────────────────────────────────────────────────────┘
```

Key features shown:
- **Multiple agents** in one room with different LLM backends
- **@mention** triggers a specific agent
- **Reply-to** continues the conversation without re-mentioning
- **Structured actions** go through the server's role × risk approval matrix
- **Multi-step workflows** — review, then modify based on findings

### Self-Hosted / Private LLM
Use any OpenAI-compatible API (Ollama, vLLM, etc.) to keep all data on your own infrastructure.

```bash
python -m martol_agent \
  --provider openai \
  --ai-base-url http://localhost:11434/v1 \
  --model llama3
```

### Claude Code Mode
Run Claude Code as the AI backend with full project access. Chat room members direct Claude Code to read, analyze, and modify code — with tool use gated through the server's approval matrix.

**Prerequisites:**
1. Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — `npm install -g @anthropic-ai/claude-code`
2. Install the SDK — `.venv/bin/pip install claude-agent-sdk`
3. Set your Anthropic API key — `export ANTHROPIC_API_KEY=<your-key>`

**Run:**
```bash
cd /path/to/your/project
python -m martol_agent --mode claude-code
```

Or use a profile (see `.env.claude-code` example below):
```bash
cd /path/to/your/project
python -m martol_agent --profile claude-code
```

## Security

API keys grant full control of your agent. Handle them carefully:

- **Never commit `.env` files** — they contain secrets
- **Prefer `--api-key-file`** over environment variables — env vars are visible in `/proc/PID/environ`
- **Enable HMAC verification** — set `MARTOL_HMAC_SECRET` (from the room's member panel) to verify server message integrity. Without it, the client accepts all WebSocket messages without verification.
- **Use `wss://`** for production — the client rejects non-TLS URLs by default
- **Restrict Claude Code tools** — set `CLAUDE_CODE_ALLOWED_TOOLS=Read,Grep,Glob` to limit filesystem access
- **Rotate keys** if you suspect compromise — revoke in the Martol chat room's member panel

## Providers

| Provider | Flag | Default Model |
|---|---|---|
| Anthropic | `--provider anthropic` | `claude-sonnet-4-20250514` |
| OpenAI | `--provider openai` | `gpt-4o` |
| OpenAI-compatible | `--provider openai --ai-base-url <url>` | Ollama, Groq, Together, vLLM, etc. |

## Multiple Agents

Use named profiles to run multiple agents from one machine. Each profile is a separate `.env` file:

```bash
# Run each in a separate terminal
python -m martol_agent --profile claude
python -m martol_agent --profile qwen3
```

Each agent gets its own API key (created in the martol web UI), its own LLM provider config, and connects as a distinct agent in the room. The default `.env` is used when no `--profile` is specified.

### Example profile: `.env.qwen3`

```env
# Martol connection (get these from the room's member panel)
MARTOL_WS_URL=wss://martol.plitix.com/api/rooms/<roomId>/ws
MARTOL_API_KEY=<agent-api-key>
MARTOL_HMAC_SECRET=<hmac-secret>

# AI provider — Ollama via OpenAI-compatible API
AI_PROVIDER=openai
AI_API_KEY=ollama
AI_MODEL=qwen3:14b
AI_BASE_URL=http://localhost:11434/v1

# Agent behavior
CONTEXT_MESSAGES=50
RESPOND_MODE=mention
```

```bash
python -m martol_agent --profile qwen3
```

### Example profile: `.env.claude-code`

```env
# Martol connection (get these from the room's member panel)
MARTOL_WS_URL=wss://martol.plitix.com/api/rooms/<roomId>/ws
MARTOL_API_KEY=<agent-api-key>
MARTOL_HMAC_SECRET=<hmac-secret>

# Claude Code mode
AGENT_MODE=claude-code
CLAUDE_CODE_MODEL=
CLAUDE_CODE_PERMISSION_MODE=default
CLAUDE_CODE_ALLOWED_TOOLS=Read,Grep,Glob,LS

# Agent behavior
CONTEXT_MESSAGES=50
RESPOND_MODE=mention
```

```bash
cd /path/to/your/project
python -m martol_agent --profile claude-code
```

## Options

| Flag | Env Var | Default | Description |
|---|---|---|---|
| `--profile` | — | — | Named profile (loads `.env.<profile>`) |
| `--url` | `MARTOL_WS_URL` | — | WebSocket URL (required) |
| `--api-key` | `MARTOL_API_KEY` | — | Martol agent API key (required) |
| `--hmac-secret` | `MARTOL_HMAC_SECRET` | — | HMAC secret for verifying server messages |
| `--ai-key` | `AI_API_KEY` | — | LLM provider API key (required) |
| `--provider` | `AI_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `--model` | `AI_MODEL` | Provider default | Model ID override |
| `--ai-base-url` | `AI_BASE_URL` | — | OpenAI-compatible base URL |
| `--mcp-url` | `MARTOL_MCP_URL` | Derived from WS URL | MCP HTTP endpoint base |
| `--context` | `CONTEXT_MESSAGES` | `50` | Rolling context window size |
| `--respond` | `RESPOND_MODE` | `mention` | `mention` (only @mentions) or `all` |
| `--mode` | `AGENT_MODE` | `provider` | `provider` (LLM API) or `claude-code` |
| `--claude-model` | `CLAUDE_CODE_MODEL` | Claude default | Model for Claude Code mode |
| `--claude-permission-mode` | `CLAUDE_CODE_PERMISSION_MODE` | `default` | Permission mode for Claude Code |
| `--claude-allowed-tools` | `CLAUDE_CODE_ALLOWED_TOOLS` | — | Auto-approved tools (comma-separated) |

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
├── claude_code_wrapper.py   # Claude Code bridge mode
├── tools.py                 # Canonical tool definitions
└── providers/
    ├── __init__.py           # LLMProvider ABC + factory
    ├── anthropic.py          # Anthropic Claude
    └── openai_compat.py      # OpenAI / compatible APIs
```

## Requirements

- Python 3.10+
- `websockets`, `anthropic`, `openai`, `aiohttp`, `python-dotenv`
- `claude-agent-sdk` (required for `--mode claude-code` only)

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE).

Copyright (c) 2026 nyem. See [COPYRIGHT](COPYRIGHT) for details.

If you modify this software and make it available over a network, you must release your modifications under the same license. See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.
