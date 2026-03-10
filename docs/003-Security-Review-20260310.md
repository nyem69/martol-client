# 003 — Security Review (2026-03-10)

**Date:** 2026-03-10
**Scope:** `martol-client` Python agent (`martol_agent/*`)
**Method:** Static code review + test run (`pytest -q`, `pytest --cov=martol_agent --cov-report=term-missing -q`)

---

## Executive Summary

| Severity | Count | Fixed |
|----------|-------|-------|
| HIGH | 2 | 2 |
| MEDIUM | 3 | 3 |
| LOW | 1 | 1 |

**All findings resolved in commit `7366a22`.**

**Top risks**
1. `WebFetch` SSRF protection can be bypassed via domain names.
2. Codex dangerous runtime options (`danger-full-access`, `never`) have no explicit safety confirmation gate.
3. IPv6 MCP URL derivation produces invalid URLs, causing operational failure for IPv6 localhost setups.

---

## HIGH

### SR-01: SSRF guard for `WebFetch` is bypassable with domain names

**Status:** FIXED (`7366a22`)

**Files:** `martol_agent/claude_code_wrapper.py:218-229`

The SSRF check blocks private/loopback/link-local only when the hostname is already a literal IP address:

- `ipaddress.ip_address(hostname)` is attempted
- domain names fall into `except ValueError` and are allowed

This means internal destinations can still be reached through domain resolution or DNS rebinding scenarios.

**Impact**
- Potential access to internal/admin endpoints via `WebFetch`
- Possible metadata/service exposure depending on runtime network

**Recommendation**
- Resolve hostname to A/AAAA records and deny if any resolved IP is private/loopback/link-local/multicast/reserved.
- Re-resolve immediately before request execution to reduce rebinding window.
- Optionally maintain an allowlist for outbound domains.

**Fix:** Now resolves domain names via `socket.getaddrinfo()` and checks all resolved IPs against private/loopback/link-local/multicast/reserved ranges. Literal IPs are still checked first for fast path.

---

### SR-02: No confirmation gate for dangerous Codex runtime settings

**Status:** FIXED (`7366a22`)

**Files:** `martol_agent/wrapper.py:527-537`, `martol_agent/codex_wrapper.py:246-248`

Codex mode accepts:
- `--codex-sandbox danger-full-access`
- `--codex-approval-policy never`

These values are passed to Codex directly, but unlike Claude `bypassPermissions`, there is no required `--...-confirm` style acknowledgment.

**Impact**
- High-risk execution profile can be enabled unintentionally
- Increased blast radius for prompt injection or misuse in shared rooms

**Recommendation**
- Add a mandatory explicit confirmation flag when either dangerous option is selected.
- Log a `CRITICAL` startup warning with full effective policy.
- Optionally block this combo unless an env flag is set for production hardening.

**Fix:** `--bypass-permissions-confirm` is now required when `--codex-sandbox=danger-full-access` or `--codex-approval-policy=never` is set. Logs `CRITICAL` with effective sandbox and approval policy.

---

## MEDIUM

### SR-03: IPv6 MCP URL derivation is invalid

**Status:** FIXED (`7366a22`)

**Files:** `martol_agent/base_wrapper.py:26-33`

`derive_mcp_url()` returns `http://::1:3000` for IPv6 localhost input, but valid URI form is `http://[::1]:3000`.
Observed repro result:

```text
derived= http://::1:3000
InvalidURL http://::1:3000/mcp/v1
```

**Impact**
- MCP calls fail in IPv6 localhost environments
- Agent appears connected but tool path is broken

**Recommendation**
- Bracket IPv6 hostnames in derived URLs: `[{parsed.hostname}]`.
- Add tests for MCP URL validity (not only string equality).

**Fix:** `derive_mcp_url()` now brackets IPv6 hostnames (detects `:` in hostname). Existing test updated to expect correct `http://[::1]:3000` form.

---

### SR-04: `action_submit.simulation` is silently stripped before MCP calls

**Status:** FIXED (`7366a22`)

**Files:** `martol_agent/tools.py:49-130`, `martol_agent/wrapper.py:56-66`

`TOOLS` schema defines `action_submit.simulation`, but `_validate_tool_args()` allowlist for `action_submit` does not include it.
Result: the field is dropped before transport.

**Impact**
- Loss of structured preview/impact metadata intended for approvers
- Reduced reviewer context may degrade approval quality

**Recommendation**
- Add `"simulation"` to `ALLOWED_TOOL_FIELDS["action_submit"]`.
- Add regression tests that verify structured fields survive validation.

**Fix:** Added `"simulation"` to `ALLOWED_TOOL_FIELDS["action_submit"]`.

---

### SR-05: Approval linkage can reference wrong triggering message

**Status:** FIXED (`7366a22`)

**Files:** `martol_agent/claude_code_wrapper.py:261-270`

Permission requests use global `self.last_known_id` as `trigger_message_id` instead of the specific initiating message ID. In active rooms, this can mismatch due to concurrent traffic.

**Impact**
- Audit records may attach to unrelated messages
- Approval traceability and forensics quality degrade

**Recommendation**
- Pass the trigger message ID through the permission flow and use that ID directly.
- Keep dbId/serverSeqId mapping logic, but start from the actual trigger.

**Fix:** Added `_current_trigger_seq` instance variable, set from the trigger payload's `serverSeqId` in `_send_to_claude()`. Permission handler now uses this instead of `self.last_known_id`, falling back to `last_known_id` only if unset.

---

## LOW

### SR-06: Tool schema tests are stale and currently failing

**Status:** FIXED (`7366a22`)

**Files:** `tests/test_tools.py:10`, `martol_agent/tools.py`

`test_tools_count` expects `len(TOOLS) == 2`, but schema now contains 4 tools (`action_submit`, `action_status`, `brief_get_active`, `brief_update`).

**Impact**
- CI/test noise obscures real regressions
- Reduced confidence in schema evolution

**Recommendation**
- Update tests to assert required tool names rather than fixed count.
- Keep converter tests but make them resilient to additive tool changes.

**Fix:** Replaced `test_tools_count` with `test_tools_have_required_names` that asserts presence of `action_submit` and `action_status` by name, resilient to additive changes.

---

## Test & Coverage Notes

**Post-fix:**
- `pytest -q`: **177 passed**
- `martol_agent/codex_wrapper.py` coverage: 32 dedicated tests added (`73c7823`)

---

## Recommended Next Actions

1. ~~Patch SR-01 and SR-02 first (security boundary issues).~~ Done (`7366a22`).
2. ~~Fix SR-03 and SR-05 next (operational correctness + audit integrity).~~ Done (`7366a22`).
3. ~~Fix SR-04 and SR-06 and add targeted tests.~~ Done (`7366a22`).
4. ~~Add dedicated tests for `codex_wrapper.py` before expanding codex-mode usage.~~ Done (`73c7823`) — 32 tests covering constructor, RPC, tool calls, lifecycle, response chunking.
5. Add regression tests for `simulation` field survival through validation.
6. Consider DNS rebinding mitigation (re-resolve before request execution).
