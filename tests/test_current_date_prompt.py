# -*- coding: utf-8 -*-
"""
Tests for the platform current-date block of a system prompt.

The block is the only place a model learns "today": without it every
date-dependent answer is a guess or a reading of a file that recorded the date
it was WRITTEN. It is emitted for EVERY employee and appended LAST, so the only
block that changes over time sits at the tail of the cached prompt prefix.
"""
from datetime import datetime, timedelta, timezone

import pytest

from core import orchestrators as orch


FIXED = datetime(2026, 11, 2, 9, 30, tzinfo=timezone(timedelta(hours=3)))

# The block itself, with its blank-line framing. A bare '## Current date'
# substring also occurs inside the tool catalog entry of current_datetime,
# so tests must match the framed block to avoid a false positive.
BLOCK_MARK = "\n\n## Current date\n\n"


@pytest.fixture()
def no_config(monkeypatch):
    """Serve an orchestrator without a stored record (built-in defaults)."""
    monkeypatch.setattr(orch, "get_orchestrator", lambda slug: None)


@pytest.fixture()
def with_config(monkeypatch):
    """Return a factory installing an orchestrator record with a given config."""
    def install(config):
        monkeypatch.setattr(
            orch, "get_orchestrator",
            lambda slug: {"prompt_text": "", "config": dict(config)},
        )
    return install


# ─── The block itself ────────────────────────────────────────────────────────


def test_block_is_appended_for_any_employee(no_config):
    out = orch._extend_prompt_with_current_date("# ROLE\nBody", "marketer", now=FIXED)
    assert out.startswith("# ROLE\nBody"), "the role prompt must stay first"
    assert "## Current date" in out


def test_block_states_the_date_in_words(no_config):
    out = orch._extend_prompt_with_current_date("x", "marketer", now=FIXED)
    assert "Today is Monday, 02 November 2026." in out


def test_block_reports_the_installation_timezone(no_config):
    out = orch._extend_prompt_with_current_date("x", "marketer", now=FIXED)
    assert "Installation timezone:" in out
    assert "UTC+03:00" in out


def test_block_carries_no_usage_instructions(no_config):
    """The block is a fact only: the system prompt is already large."""
    out = orch._extend_prompt_with_current_date("x", "marketer", now=FIXED)
    block = out.split("## Current date", 1)[1]
    assert "source of truth" not in block
    assert "Never derive" not in block


def test_block_has_no_clock_time(no_config):
    """Only the date: a per-minute timestamp would break the cached prefix."""
    out = orch._extend_prompt_with_current_date("x", "marketer", now=FIXED)
    block = out.split("## Current date", 1)[1]
    assert "09:30" not in block
    assert "9:30" not in block


def test_weekday_names_do_not_depend_on_locale(no_config):
    """Every weekday is spelled out in English, whatever the process locale."""
    for shift in range(7):
        moment = FIXED + timedelta(days=shift)
        out = orch._extend_prompt_with_current_date("x", "m", now=moment)
        expected = orch._WEEKDAY_NAMES[moment.weekday()]
        assert "Today is %s," % expected in out


def test_empty_prompt_yields_the_bare_block(no_config):
    out = orch._extend_prompt_with_current_date("", "marketer", now=FIXED)
    assert out.startswith("## Current date")
    assert not out.startswith("\n")


# ─── The on/off switch ──────────────────────────────────────────────────────


def test_block_can_be_switched_off(with_config):
    with_config({"current_date_enabled": False})
    out = orch._extend_prompt_with_current_date("# ROLE", "marketer", now=FIXED)
    assert out == "# ROLE"


@pytest.mark.parametrize("flag", ["false", "FALSE", "no", "off", "0"])
def test_string_flag_false_switches_the_block_off(with_config, flag):
    with_config({"current_date_enabled": flag})
    out = orch._extend_prompt_with_current_date("# ROLE", "marketer", now=FIXED)
    assert out == "# ROLE"


@pytest.mark.parametrize("flag", [True, "true", "yes", "on", "1", None])
def test_block_is_on_by_default(with_config, flag):
    config = {} if flag is None else {"current_date_enabled": flag}
    with_config(config)
    out = orch._extend_prompt_with_current_date("# ROLE", "marketer", now=FIXED)
    assert "## Current date" in out


# ─── Position inside the assembled prompt ───────────────────────────────────


def test_block_is_appended_after_the_tools_catalog(monkeypatch):
    """It must be the LAST block, so the cached prefix survives the day."""
    monkeypatch.setattr(orch, "get_orchestrator", lambda slug: {
        "prompt_text": "# ROLE",
        "config": {},
    })
    monkeypatch.setattr(orch, "_extend_prompt_with_skills", lambda p, s: p)
    monkeypatch.setattr(orch, "_extend_prompt_with_instructions", lambda p, s: p)
    monkeypatch.setattr(orch, "_extend_prompt_with_personal_card", lambda p, s: p)
    monkeypatch.setattr(orch, "_extend_prompt_with_memory", lambda p, s: p)
    monkeypatch.setattr(orch, "_extend_prompt_with_working_memory", lambda p, s, w="": p)
    monkeypatch.setattr(orch, "_extend_prompt_with_connections", lambda p, s: p)
    monkeypatch.setattr(orch, "_extend_prompt_with_rag_bases", lambda p, s: p)
    monkeypatch.setattr(orch, "_extend_prompt_with_delegation", lambda p, s: p)
    monkeypatch.setattr(
        orch, "_extend_prompt_with_tools",
        lambda p, s: p + "\n\n## Available tools\n\n- fake_tool",
    )
    monkeypatch.setattr(orch, "_get_services", lambda: {})

    strong, _weak = orch.build_assistant_dicts("marketer", "")
    text = strong["text"]
    assert text.index("## Available tools") < text.index(BLOCK_MARK)
    # Nothing may follow the date block: it is the tail of the prompt.
    tail = text.split(BLOCK_MARK, 1)[1].strip()
    assert tail.startswith("Today is ")
    assert tail.count("Installation timezone:") == 1
    assert "##" not in tail


def test_the_same_date_reaches_the_main_and_the_weak_assistant(monkeypatch):
    monkeypatch.setattr(orch, "get_orchestrator", lambda slug: {
        "prompt_text": "# ROLE",
        "config": {},
    })
    monkeypatch.setattr(orch, "_extend_prompt_with_skills", lambda p, s: p)
    monkeypatch.setattr(orch, "_extend_prompt_with_instructions", lambda p, s: p)
    monkeypatch.setattr(orch, "_extend_prompt_with_personal_card", lambda p, s: p)
    monkeypatch.setattr(orch, "_extend_prompt_with_memory", lambda p, s: p)
    monkeypatch.setattr(orch, "_extend_prompt_with_working_memory", lambda p, s, w="": p)
    monkeypatch.setattr(orch, "_extend_prompt_with_connections", lambda p, s: p)
    monkeypatch.setattr(orch, "_extend_prompt_with_rag_bases", lambda p, s: p)
    monkeypatch.setattr(orch, "_extend_prompt_with_delegation", lambda p, s: p)
    monkeypatch.setattr(orch, "_extend_prompt_with_tools", lambda p, s: p)
    monkeypatch.setattr(orch, "_get_services", lambda: {})

    strong, weak = orch.build_assistant_dicts("marketer", "")
    assert BLOCK_MARK in strong["text"]
    assert weak["text"] == strong["text"]


# ─── Formatting helpers ─────────────────────────────────────────────────────


def test_utc_offset_helper_formats_both_signs():
    plus = datetime(2026, 11, 2, tzinfo=timezone(timedelta(hours=3)))
    minus = datetime(2026, 11, 2, tzinfo=timezone(timedelta(hours=-5, minutes=-30)))
    assert orch._format_utc_offset(plus) == "UTC+03:00"
    assert orch._format_utc_offset(minus) == "UTC-05:30"
