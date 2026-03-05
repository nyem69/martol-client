# 002 — Comprehensive Code Review & Security Audit

**Date:** 2026-03-05
**Scope:** martol-client (Python agent) + martol server (SvelteKit/Cloudflare Workers)
**Method:** 7 parallel audit agents (Security, Database, Privacy, Network, Infrastructure, Code Quality, Devil's Advocate)

---

## Executive Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 7 |
| HIGH | 19 |
| MEDIUM | 28 |
| LOW | 17 |
| INFO | 8 |

**Top 3 risks:**
1. HMAC verification permanently accepts unsigned messages, defeating integrity checks
2. No prompt injection defenses — chat messages passed verbatim to LLM
3. Shared secret (`BETTER_AUTH_SECRET`) is single master key for all security boundaries

---

## CRITICAL

### CR-01: HMAC verification accepts unsigned messages

**Files:** `wrapper.py:309-313`, `claude_code_wrapper.py:364-368`

When `hmac_secret` is configured, messages without an `_hmac` field are accepted with a one-time warning ("migration" path). This permanently defeats HMAC verification — an attacker can omit `_hmac` to bypass all integrity checks.

**Fix:** Reject unsigned messages when `hmac_secret` is set. Add `--allow-unsigned` flag for explicit opt-in during actual migration.

### CR-02: JSON truncation produces malformed/corrupted data

**Files:** `wrapper.py:102-117`

`_sanitize_tool_result()` truncates JSON at a byte boundary and appends `"}"`. This almost always produces invalid JSON (cuts through strings, arrays, nested objects). The `json.loads()` fallback wraps raw truncated data, potentially altering the semantic meaning of tool results sent to the LLM.

**Fix:** Remove the broken `json.loads` attempt. Always use the clean fallback:
```python
return {"ok": True, "data": serialized[:MAX_TOOL_RESULT_LENGTH], "truncated": True}
```

### CR-03: `bypassPermissions` mode disables all Claude Code safety gates

**File:** `claude_code_wrapper.py:88`

`CLAUDE_CODE_PERMISSION_MODE=bypassPermissions` with an empty tool whitelist grants unrestricted shell/filesystem access. Any chat room member can trigger arbitrary command execution.

**Fix:** Refuse to start with `bypassPermissions` unless an explicit `--i-understand-the-risk` flag is also passed. Log a CRITICAL warning at startup.

### CR-04: HMAC secret falls back to BETTER_AUTH_SECRET (single master key)

**Files:** `chat-room.ts:112`, `chat-send.ts:51`, `ws/+server.ts:104`

The server uses `HMAC_SIGNING_SECRET || BETTER_AUTH_SECRET` for: broadcast signing, identity header signing, DO internal auth, AND session management. Compromising one secret compromises all security boundaries — message forgery, user impersonation, session hijacking.

**Fix:** Require `HMAC_SIGNING_SECRET` to be set independently. Fail hard on startup if missing. Never fall back to `BETTER_AUTH_SECRET`.

### CR-05: No message retention policy — unbounded table growth

**File:** `schema.ts` (messages table)

Messages accumulate indefinitely. No cleanup cron, archival mechanism, or partitioning. The table will grow unbounded, degrading query performance and increasing storage costs.

**Fix:** Add a scheduled worker that expires or archives messages older than a configurable TTL per plan tier.

### CR-06: No R2 cleanup for orphaned/deleted attachments

**Files:** `upload/+server.ts`, `chat-room.ts`

Uploaded R2 objects are never cleaned up when: uploads are abandoned (`messageId = NULL`), messages are soft-deleted, or accounts are deleted. R2 storage grows unbounded.

**Fix:** Add a cron job to delete orphaned attachments. Add R2 cleanup to account deletion and room clear flows.

### CR-07: No MCP endpoint rate limiting

**File:** `routes/mcp/v1/+server.ts`

The MCP endpoint has no rate limiting. A compromised API key can issue unlimited tool calls, exhausting Hyperdrive connections, overloading PostgreSQL, and generating unbounded Cloudflare billing.

**Fix:** Add KV-based per-API-key rate limiting (e.g., 60 requests/minute) at the `/mcp/v1` path.

---

## HIGH

### HI-01: Prompt injection — no defenses in system prompt

**Files:** `wrapper.py:502-517, 519-558`

Chat messages are passed directly to the LLM as `[senderName]: body` with no sanitization, XML wrapping, or anti-injection instructions in the system prompt. A malicious user can override agent behavior, trigger unauthorized tool calls, or exfiltrate the system prompt.

**Fix:** Add injection-resistance instructions to the system prompt. Wrap user messages in `<user_message>` tags. Add output validation on tool calls.

### HI-02: Full conversation context sent to third-party LLM providers

**Files:** `wrapper.py:519-558`, `providers/anthropic.py:38`, `providers/openai_compat.py:53`

The rolling context window (default 50 messages) including all sender names and message bodies is sent verbatim to Anthropic/OpenAI. This includes messages from users who never addressed the agent, especially in `--respond all` mode.

**Fix:** Pseudonymize sender names before sending to LLM. Consider filtering context to only relevant conversation threads.

### HI-03: Claude Code "safe" tools enable sensitive file reads

**Files:** `claude_code_wrapper.py:225`

Default safe tools (`Read`, `Grep`, `Glob`, `LS`) can read any file on the system: `.env`, `~/.ssh/id_rsa`, `~/.aws/credentials`, `/etc/passwd`. `WebFetch` enables SSRF against internal endpoints (e.g., cloud metadata at `169.254.169.254`).

**Fix:** Add path deny-list patterns (`.env*`, `*.key`, `*.pem`, `~/.ssh/*`). Block internal IP ranges for WebFetch.

### HI-04: No per-user AI opt-out mechanism

**Files:** `wrapper.py:274-281`, `PRIVACY.md`

Room members cannot prevent their messages from being processed by AI agents. The only recourse is leaving the room, effectively forcing consent.

**Fix:** Add per-user AI opt-out at the room level. Exclude opted-out users' messages from LLM context.

### HI-05: Real sender names (PII) included in LLM context

**File:** `wrapper.py:542`

`content = f"[{sender_name}]: {body}"` — real usernames are transmitted to third-party AI providers as part of every LLM call.

**Fix:** Use pseudonymized identifiers (`User-1`, `User-2`) in LLM context. Maintain a local mapping for display names in responses.

### HI-06: `safeSend` bypasses HMAC signing for unicast messages

**File:** `chat-room.ts:1237-1243`

`safeSend()` sends messages directly without HMAC signing. Roster updates, error messages, history sync, and system messages are all unsigned. Combined with CR-01, this creates a wide attack surface for message injection.

**Fix:** Sign all messages via a shared signing helper, not just broadcasts.

### HI-07: No timeout on LLM provider API calls

**Files:** `providers/anthropic.py:38`, `providers/openai_compat.py:53`

Both SDKs default to 600-second (10-minute) timeouts. During this time, the `_responding` lock is held, blocking all message processing. With 5 tool iterations, the agent could be unresponsive for 50 minutes.

**Fix:** Set explicit `timeout=60` on both SDK clients. Wrap `_generate_response` with `asyncio.wait_for(timeout=120)`.

### HI-08: No TLS enforcement for MCP HTTP URL

**Files:** `wrapper.py:137-146, 757`

The `--mcp-url` CLI argument accepts arbitrary URLs, bypassing the TLS enforcement applied to WebSocket URLs. API keys in `x-api-key` headers are sent in plaintext over HTTP.

**Fix:** Apply the same TLS enforcement check to MCP URLs.

### HI-09: Connection drop during tool loop leaves inconsistent state

**File:** `wrapper.py:560-600`

If WebSocket disconnects mid-tool-loop: MCP actions may be submitted via HTTP (succeeds) but responses can't be delivered (WS down). Orphaned pending actions with no client follow-up.

**Fix:** Check WebSocket connectivity before each tool loop iteration. Add startup reconciliation for orphaned pending actions.

### HI-10: Tool input/output logged at INFO level with content

**File:** `wrapper.py:580-582`

Tool arguments and results (up to 200 chars) are logged at default INFO level, including action descriptions, code snippets, file paths, and message bodies.

**Fix:** Move to DEBUG level. Log only metadata (tool name, result status, byte count) at INFO.

### HI-11: Claude Code prompt logged with sender name and content

**File:** `claude_code_wrapper.py:451`

`log.info("Sending to Claude Code: %s", prompt[:120])` logs sender PII and message content at default log level.

**Fix:** Move to DEBUG. Log only character count at INFO.

### HI-12: Permission request payloads broadcast to chat room

**File:** `claude_code_wrapper.py:261-298`

Full tool inputs (file paths, commands, file contents) are posted to the chat room and stored in the `pending_actions` table. Sensitive filesystem data visible to all room members.

**Fix:** Truncate and sanitize tool inputs before broadcasting. Omit file content for Write operations.

### HI-13: Missing foreign keys on 18+ application table columns

**File:** `schema.ts`

Multiple columns referencing `user.id` and `organization.id` lack FK constraints: `messages.senderId`, `pendingActions.requestedBy`, `agentRoomBindings.agentUserId`, `termsAcceptances.userId`, etc. Orphaned records accumulate; GDPR deletion is fragile.

**Fix:** Add FK constraints with appropriate `ON DELETE` actions in a new migration.

### HI-14: Agent deletion leaves orphaned user/account rows

**File:** `routes/api/agents/[id]/+server.ts:82-91`

`DELETE /api/agents/[id]` deletes `apikey` and `member` rows but not the synthetic `user` and `account` rows created during agent creation.

**Fix:** Delete `account` and `user` rows for the agent in the delete handler, wrapped in a transaction.

### HI-15: Agent creation — API key step outside transaction

**File:** `routes/api/agents/+server.ts:67-116`

User/account/member creation is transactional, but `createApiKey()` runs outside the transaction. Failure leaves an agent user without a key, and the recovery path (revoke) has the orphan problem from HI-14.

**Fix:** Add compensating cleanup (delete user/account/member) if API key creation fails.

### HI-16: Username change is not atomic

**File:** `routes/api/account/username/+server.ts:147-174`

Three writes (update username, insert history, insert audit) run as independent queries with no transaction. Partial failure leaves inconsistent data.

**Fix:** Wrap in `db.transaction()`.

### HI-17: KV rate limiter fails open + has race conditions

**Files:** `rate-limit.ts:41-64, 76-79`

Read-modify-write race condition allows 2-10x configured limit under concurrent load. KV failure returns `allowed: true`. For OTP verification (5 attempts/15 minutes), this could allow 10-50 attempts.

**Fix:** Consider DO-based rate limiting for critical auth paths. Fail closed for OTP verification.

### HI-18: Approval polling blocks agent for up to 5 minutes

**File:** `claude_code_wrapper.py:326-343`

`_wait_for_approval()` holds the `_responding` lock for up to 300 seconds. The agent is completely unresponsive during this time.

**Fix:** Reduce timeout to 60s. Release the lock during polling. Send periodic "waiting for approval" messages.

### HI-19: Observability sampling at 100% in production

**File:** `wrangler.toml:18-31`

`head_sampling_rate = 1` with `persist = true` for all logs. Every request generates persisted observability data, leading to unexpected billing at scale.

**Fix:** Reduce to 1-10% for production. Keep 100% only for debugging.

---

## MEDIUM

### ME-01: TLS bypass via substring matching

**Files:** `wrapper.py:198`, `claude_code_wrapper.py:122`

TLS check uses `"localhost" not in self.ws_url` — a URL like `ws://evil.com/localhost/ws` bypasses it.

**Fix:** Parse URL and check only `parsed.hostname`.

### ME-02: No WebSocket connect/message size limits on client

**Files:** `wrapper.py:207, 287`

No explicit `open_timeout`, `close_timeout`, or `max_size` on `websockets.connect()`. Default 1MB max_size exists but isn't declared. Missing connect timeout could cause hangs.

**Fix:** Set `open_timeout=10`, `close_timeout=5`, `max_size=1_048_576`.

### ME-03: New aiohttp session per MCP call

**Files:** `wrapper.py:654`, `claude_code_wrapper.py:576`

Every `_mcp_call()` creates a new TCP connection with TLS handshake. Approval polling creates up to 100 connections over 5 minutes.

**Fix:** Create a single `aiohttp.ClientSession` in `__init__`, reuse across calls, close in `_shutdown()`.

### ME-04: No CHECK constraints on enumerated text columns

**File:** `schema.ts`

Columns like `status`, `actionType`, `riskLevel`, `senderRole` use text with TypeScript-only type guards. Database accepts any string.

**Fix:** Add PostgreSQL CHECK constraints via migration.

### ME-05: N+1 query pattern in terms acceptance (6 queries per page load)

**File:** `hooks.server.ts:148-178`

Terms re-acceptance check runs 6 sequential queries on every authenticated page request.

**Fix:** Combine into a single LEFT JOIN query. Cache latest terms versions in KV.

### ME-06: Unbounded queries use fetch-all + `.length` instead of COUNT(*)

**Files:** `feature-gates.ts:66-76`, `billing/webhook/+server.ts:62-66`, `billing/checkout/+server.ts:78-82`

Member counts, agent counts, and subscription counts are computed by fetching all rows and checking `.length`.

**Fix:** Use `SELECT COUNT(*)` aggregate queries.

### ME-07: Data export fetches unbounded message history

**File:** `routes/api/account/export/+server.ts:82`

GDPR export fetches ALL messages by a user without limit. Could exhaust Worker memory (128MB limit).

**Fix:** Add pagination or generate exports as background jobs.

### ME-08: No rollback migrations

**File:** `drizzle/` (all migration files)

8 forward-only migrations with no corresponding down/rollback scripts. Bad migrations require manual SQL intervention.

**Fix:** Maintain manual rollback scripts, especially for destructive operations.

### ME-09: NOT NULL column added without DEFAULT

**File:** `drizzle/0006_dashing_black_widow.sql:4`

`ALTER TABLE "attachments" ADD COLUMN "uploaded_by" text NOT NULL;` — fails on populated tables.

**Fix:** Always provide DEFAULT or add nullable first, backfill, then add NOT NULL.

### ME-10: WebSocket ping/pong mismatch with Cloudflare Hibernation

**Files:** `wrapper.py:207`, `chat-room.ts` (Hibernation API)

Client's default ping interval (20s) may not be answered by hibernated DOs, causing spurious reconnections that reset agent context.

**Fix:** Tune `ping_interval=30, ping_timeout=30` on client. Verify Cloudflare's Hibernation API handles protocol-level pings.

### ME-11: Delta sync gap when DO prunes WAL

**Files:** `wrapper.py:203`, `chat-room.ts:754-782`

If agent disconnects long enough for WAL pruning (>200 messages), delta sync misses the gap. Client has no way to detect this.

**Fix:** Server should send a `gap` notification when `lastKnownId` is older than oldest WAL entry, triggering full resync.

### ME-12: Reply-to uses DO IDs but action_submit expects DB IDs

**Files:** `wrapper.py:569`, `action-submit.ts:87`

WebSocket messages carry `serverSeqId` (DO-local), but `action_submit` looks up `trigger_message_id` in PostgreSQL using BIGSERIAL IDs. LLM will consistently provide wrong IDs for live messages.

**Fix:** Accept `serverSeqId` in `action_submit` and resolve internally, or store `dbId` from `id_map` broadcasts in conversation context.

### ME-13: Fire-and-forget tasks accumulate under load

**Files:** `wrapper.py:357`, `claude_code_wrapper.py:408`

`asyncio.create_task(_generate_response())` queues unbounded tasks. Under high volume, stale-context responses fire sequentially.

**Fix:** Skip if `_responding` lock is already held, or use a bounded queue with debounce.

### ME-14: No user feedback during LLM API outage

**File:** `wrapper.py:487-500`

When the LLM API fails, users see typing indicator for 30+ seconds then silence. No message indicates the agent is unable to respond. Failed calls still consume rate limiter tokens.

**Fix:** Send a fallback error message. Don't consume rate limit tokens for failed calls.

### ME-15: TOCTOU race in username uniqueness

**File:** `routes/api/account/username/+server.ts:111-155`

SELECT-then-UPDATE race. Concurrent requests could both pass uniqueness check. DB unique index prevents corruption but produces an ugly 500 error.

**Fix:** Catch unique constraint violations and return friendly error.

### ME-16: Turnstile CAPTCHA fails open

**File:** `hooks.server.ts:251-254`

Network error during Turnstile verification allows OTP send to proceed. Rate limiting provides secondary defense.

**Fix:** Consider failing closed, or at minimum log a WARN alert.

### ME-17: OTP logged to console in dev mode

**File:** `auth/index.ts:78`

`console.warn([Auth] DEV ONLY — OTP for ${email}: ${otp})` — guard relies on URL containing "localhost", not a proper environment flag.

**Fix:** Add explicit `ENVIRONMENT !== 'production'` guard.

### ME-18: Conversation context lacks data minimization

**Files:** `wrapper.py:386-416, 267`

All messages within the window (regardless of relevance, age, or topic) are sent to the LLM on every call. Messages from before the agent joined, or on unrelated topics, are unnecessarily shared.

**Fix:** Filter to relevant threads. Consider time-based window in addition to count-based.

### ME-19: IP addresses stored without retention policy

**Files:** `schema.ts:251,286`, `auth-schema.ts:39`

IP addresses in `session`, `accountAudit`, and `termsAcceptances` tables stored indefinitely. GDPR data minimization violation.

**Fix:** Define retention period (e.g., 90 days). Add cleanup cron.

### ME-20: `.env` files on disk with default permissions

**Files:** `.env`, `.env.claude-code`, `.env.qwen3`

Live secrets in files with default 644 permissions (world-readable on multi-user systems).

**Fix:** `chmod 600 .env*`. Document `--api-key-file` as preferred approach.

### ME-21: Duplicate WebSocket upgrade handlers

**Files:** `worker-entry.ts`, `routes/api/rooms/[roomId]/ws/+server.ts`

Two code paths for WebSocket upgrades. SvelteKit route is dead code (intercepted by worker-entry.ts first). Security fix in one won't propagate to the other.

**Fix:** Remove the dead SvelteKit route handler.

### ME-22: ~300 lines of duplicated code across two wrappers

**Files:** `wrapper.py`, `claude_code_wrapper.py`

`connect()`, `_listen()`, `_verify_hmac()`, `_handle_message()`, `_mcp_call()`, `send_message()`, etc. are near-identical copies. Bug fixes must be applied to both files manually.

**Fix:** Extract `BaseWrapper` class with shared logic.

### ME-23: `_validate_tool_args` passes through unknown tools

**File:** `wrapper.py:89-99`

Unknown tool names get all arguments passed through unchanged. Combined with prompt injection, arbitrary tool calls could reach the MCP server.

**Fix:** Return empty dict for unknown tool names.

### ME-24: Alarm backoff can reach 1 hour

**File:** `chat-room.ts:348-352`

DB flush backoff caps at 1 hour. Messages in DO WAL are invisible to HTTP reads/new connections during this period.

**Fix:** Reduce max backoff to 5-10 minutes. Add manual `/flush` recovery command.

### ME-25: Stripe webhook lacks IP validation

**File:** `routes/api/billing/webhook/+server.ts`

Signature verification is present (good) but no source IP filtering. Enables DoS via crafted requests that fail verification but consume CPU.

**Fix:** Add Cloudflare WAF rules restricting the webhook path to Stripe's IP ranges.

### ME-26: No pending action expiry

**Files:** `schema.ts` (`expired` status exists), cron handler

The `expired` status is defined but no code ever sets it. Pending actions accumulate indefinitely.

**Fix:** Add cron job to expire actions older than 24h.

### ME-27: claude-agent-sdk version not pinned

**File:** `requirements.txt`

`claude-agent-sdk>=0.1.0` with no upper bound. The 0.x SDK may break API between minor versions.

**Fix:** Pin to `claude-agent-sdk>=0.1.0,<0.2.0`.

### ME-28: Critical context eviction in 50-message window

**File:** `wrapper.py:386-416`

During approval polling (up to 5 min), 50+ messages could evict the original request context. `_is_reply_to_self()` also fails when the original agent message is evicted.

**Fix:** Separate LLM context window from reply-detection index. Keep agent messages and pending-action-related messages longer.

---

## LOW

### LO-01: API key visible in process arguments (`ps aux`)
`wrapper.py:748-750` — Document `--api-key-file` as preferred.

### LO-02: `_is_mentioned` false positives on common names
`wrapper.py:459-475` — Substring match triggers on names like "claude" in any context. Use word boundary regex.

### LO-03: No client-side message body size check
`wrapper.py:688` — Oversized messages waste bandwidth (server rejects at 32KB).

### LO-04: No redirect handling in MCP HTTP client
`wrapper.py:654` — aiohttp follows redirects by default, potentially leaking `x-api-key` to redirect targets. Set `allow_redirects=False`.

### LO-05: `derive_mcp_url` doesn't validate hostname
`wrapper.py:137-146` — Malformed URLs produce `https://None` as MCP URL.

### LO-06: Binary WebSocket frames silently ignored by server
`chat-room.ts:271` — No error feedback for binary frames.

### LO-07: Message chunking has no inter-chunk delay
`claude_code_wrapper.py:471-474` — Long responses could hit server rate limit (10 msg/1000ms).

### LO-08: No duplicate detection on client side
`wrapper.py:339-366` — Duplicates during reconnection could appear in LLM context.

### LO-09: Unconstrained text IDs (no length limit)
`auth-schema.ts` — All PKs use unconstrained `text`. Low practical risk.

### LO-10: Boolean emulated as integer in subscriptions
`schema.ts:343-345` — `foundingMember` and `cancelAtPeriodEnd` use integer 0/1 instead of boolean.

### LO-11: Local dev pool has no timeout configuration
`direct.ts:20-28` — Dev-only; no production impact.

### LO-12: `rejectUnauthorized: false` in dev configs
`drizzle.config.ts:16`, `direct.ts:26` — TLS cert verification disabled for dev/migration.

### LO-13: No `Vary: Origin` header on CORS responses
`hooks.server.ts:451-454` — Cache poisoning risk with intermediate proxies.

### LO-14: Cloudflare infra IDs in wrangler.toml
`wrangler.toml` — Account ID, KV namespace ID, Hyperdrive ID committed to git. Not secrets but aids reconnaissance.

### LO-15: No minimum TLS version enforcement
`wrapper.py:201` — Python defaults to TLS 1.2+. Cloudflare enforces TLS 1.2 at edge anyway.

### LO-16: Wrangler.toml Turnstile site key committed
Intentionally public (client-side key). No action needed.

### LO-17: Error messages may leak internal state via exc_info logging
`wrapper.py:498` — Full stack traces at INFO level could contain sensitive context.

---

## INFO (Positive Findings)

### IN-01: All database queries use parameterized ORM
No SQL injection vectors found. Drizzle ORM's query builder and `sql` tagged templates properly bind parameters.

### IN-02: HMAC cryptographic implementation is sound
Server uses Web Crypto API with SHA-256. Client uses `hmac.compare_digest()` for timing-safe comparison. The signing/verification logic itself is correct (the issue is the bypass in CR-01).

### IN-03: Read cursor upsert uses GREATEST() correctly
Prevents cursor regression. Well-implemented idempotent pattern.

### IN-04: Security headers properly configured
HSTS (2 years, includeSubDomains, preload), X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy all present.

### IN-05: CORS uses allowlist, not wildcard
Explicit `Set` of allowed origins. No `*` usage.

### IN-06: R2 upload validates magic bytes + blocks SVG
File type verification prevents stored XSS via SVG uploads.

### IN-07: Degraded mode with self-healing is well-implemented
3-failure threshold, exponential backoff, continued retry, message rejection to prevent WAL overflow.

### IN-08: martol-client has zero direct database access
All data access mediated by server API with authentication/authorization.

---

## Priority Remediation

### Immediate (Week 1)
1. **CR-01** — Reject unsigned messages when HMAC is configured
2. **CR-02** — Fix JSON truncation to always use clean fallback
3. **CR-04** — Separate HMAC signing key from BETTER_AUTH_SECRET
4. **HI-01** — Add prompt injection resistance to system prompt
5. **HI-07** — Add explicit timeouts to LLM SDK clients
6. **HI-08** — Enforce TLS for MCP HTTP URL

### Short-term (Week 2-3)
7. **CR-07** — Add MCP endpoint rate limiting
8. **HI-06** — Sign all server messages (not just broadcasts)
9. **HI-13** — Add FK constraints to application tables
10. **HI-14/15** — Fix agent creation/deletion atomicity
11. **HI-16** — Wrap username change in transaction
12. **ME-03** — Reuse aiohttp session across MCP calls
13. **ME-22** — Extract BaseWrapper to eliminate code duplication

### Medium-term (Month 1-2)
14. **CR-05** — Implement message retention policy
15. **CR-06** — Add R2 orphan cleanup
16. **HI-02/05** — Pseudonymize sender names in LLM context
17. **HI-03** — Add path deny-list for Claude Code tools
18. **ME-01** — Fix TLS bypass to use proper URL parsing
19. **ME-19** — Define and implement IP address retention policy
20. **ME-26** — Add pending action expiry cron

### Long-term
21. **HI-04** — Per-user AI opt-out mechanism
22. **CR-03** — Add safety guard for bypassPermissions mode
23. **ME-21** — Remove duplicate WebSocket upgrade handler
24. **HI-17** — Replace KV rate limiting with DO-based for auth paths
