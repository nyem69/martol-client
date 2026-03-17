"""
Tiered Context Budgeting for agent system prompts.

Implements a 3-tier degradation strategy for the room brief:
  1. Full render with structured sections + headings
  2. Per-section truncation at BRIEF_SECTION_MAX_CHARS with ellipsis
  3. Stub fallback directing the agent to call brief_get_active

See docs/021-Context-Budgeting.md in the martol repo for the full spec.
"""

import json
import logging
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger("martol-agent")

# ── Constants ────────────────────────────────────────────────────────

DEFAULT_CONTEXT_BUDGET_CHARS = 80_000
DEFAULT_BRIEF_MAX_CHARS = 8_000
DEFAULT_BRIEF_SECTION_MAX_CHARS = 1_500

BRIEF_STUB = "Project brief available \u2014 call brief_get_active to refresh"

BRIEF_SECTIONS = [
    ("goal", "Goal"),
    ("stack", "Stack"),
    ("conventions", "Conventions"),
    ("phase", "Current Phase"),
    ("notes", "Notes"),
]


# ── Types ────────────────────────────────────────────────────────────

class TierLevel(Enum):
    """Degradation tier for context content."""
    ESSENTIAL = "essential"
    STANDARD = "standard"
    OPTIONAL = "optional"


@dataclass
class ContextBudget:
    """Budget configuration for system prompt assembly."""
    total_chars: int = DEFAULT_CONTEXT_BUDGET_CHARS
    brief_max: int = DEFAULT_BRIEF_MAX_CHARS
    section_max: int = DEFAULT_BRIEF_SECTION_MAX_CHARS


# ── Measurement ──────────────────────────────────────────────────────

def measure(text: str) -> int:
    """Measure text size in characters. Isolated for future swap to token counting."""
    return len(text)


# ── Brief Rendering ─────────────────────────────────────────────────

def _render_sections(parsed: dict, max_section: int | None = None) -> str:
    """Render brief sections with headings, optionally truncating each section."""
    parts: list[str] = []
    for key, heading in BRIEF_SECTIONS:
        val = parsed.get(key, "")
        if not val:
            continue
        if max_section is not None and measure(val) > max_section:
            val = val[:max_section] + "..."
        parts.append(f"## {heading}\n{val}")
    return "\n\n".join(parts)


def render_brief(brief_json: str | None, budget: ContextBudget) -> tuple[str, TierLevel]:
    """Render a room brief with 3-step degradation.

    Args:
        brief_json: Raw brief string (may be JSON with structured sections, or plain text).
        budget: Budget configuration with brief_max and section_max limits.

    Returns:
        Tuple of (rendered_text, tier_level).
        - STANDARD tier: full or truncated render succeeded within brief_max
        - OPTIONAL tier: stub fallback (brief too large even after truncation)
        - ESSENTIAL tier is never returned here (brief is STANDARD content)
    """
    if not brief_json:
        return ("", TierLevel.STANDARD)

    # Try to parse as structured JSON brief
    parsed = None
    try:
        parsed = json.loads(brief_json)
        if not isinstance(parsed, dict) or "goal" not in parsed:
            parsed = None
    except (ValueError, TypeError):
        parsed = None

    if parsed is None:
        # Plain text brief — apply simple truncation
        if measure(brief_json) <= budget.brief_max:
            return (brief_json, TierLevel.STANDARD)
        truncated = brief_json[:budget.brief_max] + "..."
        if measure(truncated) <= budget.brief_max + 3:
            log.info("Brief degraded: plain text truncated to %d chars", budget.brief_max)
            return (truncated, TierLevel.STANDARD)
        log.info("Brief degraded: stub fallback")
        return (BRIEF_STUB, TierLevel.OPTIONAL)

    # Step 1: Full render (no truncation)
    full = _render_sections(parsed)
    if measure(full) <= budget.brief_max:
        return (full, TierLevel.STANDARD)

    # Step 2: Per-section truncation
    truncated = _render_sections(parsed, max_section=budget.section_max)
    if measure(truncated) <= budget.brief_max:
        log.info("Brief degraded: per-section truncation (%d chars)", measure(truncated))
        return (truncated, TierLevel.STANDARD)

    # Step 3: Stub fallback
    log.info("Brief degraded: stub fallback (truncated was %d chars, limit %d)",
             measure(truncated), budget.brief_max)
    return (BRIEF_STUB, TierLevel.OPTIONAL)


# ── Budget Check ─────────────────────────────────────────────────────

def check_budget(prompt: str, budget: ContextBudget) -> tuple[bool, int]:
    """Check whether a prompt is within the total character budget.

    Returns:
        Tuple of (within_budget, chars_used).
    """
    chars_used = measure(prompt)
    return (chars_used <= budget.total_chars, chars_used)
