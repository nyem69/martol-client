"""Tests for martol_agent.context_budget — tiered context budgeting."""

import json

import pytest

from martol_agent.context_budget import (
    BRIEF_STUB,
    ContextBudget,
    TierLevel,
    check_budget,
    measure,
    render_brief,
)


# ── measure() ───────────────────────────────────────────────────────


class TestMeasure:
    def test_empty(self):
        assert measure("") == 0

    def test_ascii(self):
        assert measure("hello") == 5

    def test_unicode(self):
        assert measure("héllo") == 5

    def test_multiline(self):
        assert measure("line1\nline2") == 11


# ── render_brief() ──────────────────────────────────────────────────


class TestRenderBrief:
    @pytest.fixture
    def budget(self):
        return ContextBudget()

    @pytest.fixture
    def small_budget(self):
        """Tight budget for testing truncation and fallback."""
        return ContextBudget(brief_max=200, section_max=50)

    # -- No brief --

    def test_none_brief_returns_empty(self, budget):
        text, tier = render_brief(None, budget)
        assert text == ""
        assert tier == TierLevel.STANDARD

    def test_empty_string_brief_returns_empty(self, budget):
        text, tier = render_brief("", budget)
        assert text == ""
        assert tier == TierLevel.STANDARD

    # -- Short brief (under limit) --

    def test_short_json_brief_full_render(self, budget):
        brief = json.dumps({
            "goal": "Build a chat app",
            "stack": "SvelteKit + PostgreSQL",
            "conventions": "Tabs, runes only",
            "phase": "MVP",
            "notes": "Ship fast",
        })
        text, tier = render_brief(brief, budget)
        assert tier == TierLevel.STANDARD
        assert "## Goal" in text
        assert "Build a chat app" in text
        assert "## Stack" in text
        assert "SvelteKit + PostgreSQL" in text
        assert "## Conventions" in text
        assert "## Current Phase" in text
        assert "## Notes" in text

    # -- At limit --

    def test_brief_exactly_at_limit(self):
        """Brief that renders to exactly brief_max should use STANDARD tier."""
        goal_text = "x"  # will be padded
        # Build a brief, measure it, then set brief_max to match
        brief = json.dumps({"goal": goal_text})
        temp_budget = ContextBudget(brief_max=100_000)
        rendered, _ = render_brief(brief, temp_budget)
        exact_len = measure(rendered)

        budget = ContextBudget(brief_max=exact_len)
        text, tier = render_brief(brief, budget)
        assert tier == TierLevel.STANDARD
        assert text == rendered

    # -- Over limit (section truncation) --

    def test_over_limit_triggers_section_truncation(self):
        """A brief with one very long section should be per-section truncated."""
        long_section = "A" * 5000
        brief = json.dumps({
            "goal": long_section,
            "stack": "short",
        })
        # Set brief_max so full render exceeds it but truncated fits
        budget = ContextBudget(brief_max=2000, section_max=500)
        text, tier = render_brief(brief, budget)
        assert tier == TierLevel.STANDARD
        # The goal section should be truncated with ellipsis
        assert "..." in text
        # Should not contain the full long_section
        assert long_section not in text
        # Headings still present
        assert "## Goal" in text
        assert "## Stack" in text

    # -- Way over (stub fallback) --

    def test_way_over_limit_falls_back_to_stub(self):
        """Brief so large that even truncated sections exceed limit -> stub."""
        # Make every section very long
        big = "Z" * 10000
        brief = json.dumps({
            "goal": big,
            "stack": big,
            "conventions": big,
            "phase": big,
            "notes": big,
        })
        # Very tight budget that even truncated sections won't fit
        budget = ContextBudget(brief_max=100, section_max=50)
        text, tier = render_brief(brief, budget)
        assert tier == TierLevel.OPTIONAL
        assert text == BRIEF_STUB

    # -- Malformed JSON --

    def test_malformed_json_plain_text_under_limit(self, budget):
        """Non-JSON string under limit is returned as-is."""
        raw = "This is not JSON, just plain text."
        text, tier = render_brief(raw, budget)
        assert text == raw
        assert tier == TierLevel.STANDARD

    def test_malformed_json_plain_text_over_limit(self):
        """Non-JSON string over limit is truncated."""
        raw = "X" * 500
        budget = ContextBudget(brief_max=100)
        text, tier = render_brief(raw, budget)
        assert tier == TierLevel.STANDARD
        assert text.endswith("...")
        assert len(text) <= 103  # 100 + "..."

    def test_json_without_goal_treated_as_plain_text(self, budget):
        """JSON dict missing the 'goal' key is treated as plain text."""
        brief = json.dumps({"stack": "Python", "notes": "testing"})
        text, tier = render_brief(brief, budget)
        # Treated as plain text — returned as the raw JSON string
        assert text == brief
        assert tier == TierLevel.STANDARD

    # -- Missing sections --

    def test_missing_sections_renders_available_only(self, budget):
        """JSON with only some sections renders only those."""
        brief = json.dumps({"goal": "Ship v2"})
        text, tier = render_brief(brief, budget)
        assert tier == TierLevel.STANDARD
        assert "## Goal" in text
        assert "Ship v2" in text
        # Absent sections should not appear
        assert "## Stack" not in text
        assert "## Conventions" not in text
        assert "## Current Phase" not in text
        assert "## Notes" not in text

    def test_partial_sections(self, budget):
        """JSON with goal + notes but nothing else."""
        brief = json.dumps({"goal": "Migrate DB", "notes": "Use Drizzle"})
        text, tier = render_brief(brief, budget)
        assert tier == TierLevel.STANDARD
        assert "## Goal" in text
        assert "## Notes" in text
        assert "## Stack" not in text


# ── check_budget() ──────────────────────────────────────────────────


class TestCheckBudget:
    def test_within_budget(self):
        budget = ContextBudget(total_chars=100)
        prompt = "a" * 50
        within, chars = check_budget(prompt, budget)
        assert within is True
        assert chars == 50

    def test_exactly_at_budget(self):
        budget = ContextBudget(total_chars=100)
        prompt = "a" * 100
        within, chars = check_budget(prompt, budget)
        assert within is True
        assert chars == 100

    def test_over_budget(self):
        budget = ContextBudget(total_chars=100)
        prompt = "a" * 101
        within, chars = check_budget(prompt, budget)
        assert within is False
        assert chars == 101

    def test_empty_prompt(self):
        budget = ContextBudget(total_chars=100)
        within, chars = check_budget("", budget)
        assert within is True
        assert chars == 0
