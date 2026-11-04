# -*- coding: utf-8 -*-
"""tests/test_universal_agent_delegation.py - the right seen and the right enforced.

A prompt that lists an agent the runtime refuses is a dead end, and a
runtime that accepts an agent the prompt never mentions is a right nobody
uses. Both sides read the effective list, and these tests pin that they
agree: before the checkbox is cleared the executor is spawnable, after it
the call is refused with a reason the model can act on.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.db import reset_devagent_engine, reset_engine  # noqa: E402

SLUG = "ua_parent"


@pytest.fixture
def platform(tmp_path, monkeypatch):
    """A fresh data dir so orchestrator rows and folders are isolated."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))

    import core.paths as paths_mod
    for attr, value in (("DATA_DIR", str(tmp_path)),
                        ("DB_PATH", str(tmp_path / "sagaai.db")),
                        ("DEVAGENT_DB_PATH", str(tmp_path / "devagent.db")),
                        ("HISTORY_DIR", str(tmp_path / "history")),
                        ("SYSTEM_PROMPTS_DIR", str(tmp_path / "system_prompts"))):
        monkeypatch.setattr(paths_mod, attr, value, raising=False)

    reset_engine()
    reset_devagent_engine()
    yield tmp_path
    reset_engine()
    reset_devagent_engine()


@pytest.fixture
def parent(platform):
    """An ordinary employee plus the bundled executor in the same store."""
    from core.orchestrators import UNIVERSAL_AGENT_SLUG, create_orchestrator

    assert create_orchestrator(SLUG, "Parent Employee", "For tests") is not None
    assert create_orchestrator(UNIVERSAL_AGENT_SLUG, "Universal-Agent",
                               "For tests") is not None
    return SLUG


# ─── enforcement ───────────────────────────────────────────────────────────


def test_executor_is_spawnable_by_default(parent):
    from core.orchestration.tools import check_agent_allowed
    from core.orchestrators import UNIVERSAL_AGENT_SLUG

    assert check_agent_allowed(parent, UNIVERSAL_AGENT_SLUG) is None


def test_switching_off_refuses_the_spawn(parent):
    """The refusal carries the reason and the list the model must use next."""
    from core.orchestration.tools import check_agent_allowed
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        set_universal_agent_enabled,
    )

    set_universal_agent_enabled(parent, False)
    refusal = check_agent_allowed(parent, UNIVERSAL_AGENT_SLUG)

    assert refusal is not None
    assert refusal.get("ok") is False
    assert UNIVERSAL_AGENT_SLUG not in refusal.get("allowed", [])


def test_unknown_agent_still_refused_with_the_executor_available(parent):
    """The default right never widens the list to arbitrary slugs."""
    from core.orchestration.tools import check_agent_allowed

    assert check_agent_allowed(parent, "no_such_employee") is not None


def test_self_only_is_false_while_the_executor_is_available(parent):
    """The default right is the difference between self-only and not."""
    from core.orchestration.tools import _allowlist_context
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        set_universal_agent_enabled,
    )

    assert _allowlist_context(parent)["self_only"] is False
    set_universal_agent_enabled(parent, False)
    ctx = _allowlist_context(parent)
    assert ctx["self_only"] is True
    assert UNIVERSAL_AGENT_SLUG not in ctx["allowed"]


# ─── the prompt block ──────────────────────────────────────────────────────


def test_prompt_block_lists_the_executor_by_default(parent):
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        _extend_prompt_with_delegation,
    )

    block = _extend_prompt_with_delegation("Base prompt.", parent)

    assert "## Available agents" in block
    assert UNIVERSAL_AGENT_SLUG in block
    assert "available by default" in block


def test_prompt_block_drops_it_after_switching_off(parent):
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        _extend_prompt_with_delegation,
        set_universal_agent_enabled,
    )

    set_universal_agent_enabled(parent, False)
    block = _extend_prompt_with_delegation("Base prompt.", parent)

    assert UNIVERSAL_AGENT_SLUG not in block


def test_prompt_block_keeps_configured_agents_alongside(parent):
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        _extend_prompt_with_delegation,
        set_allowed_agents,
    )

    set_allowed_agents(parent, ["research_agent"])
    block = _extend_prompt_with_delegation("Base prompt.", parent)

    assert "research_agent" in block
    assert UNIVERSAL_AGENT_SLUG in block
