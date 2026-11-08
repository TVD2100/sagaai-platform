# -*- coding: utf-8 -*-
"""tests/test_universal_agent.py - the bundled Universal-Agent and its default right.

The executor is available to every orchestrator out of the box and is
switched off per orchestrator with a checkbox. The properties pinned here
are the ones that would make that promise silently false: the flag must
read as ON when the config says nothing (a fresh employee never writes the
key), OFF only after an explicit decision, and the effective list - the one
the runtime enforces and the prompt advertises - must follow the flag
without ever duplicating an entry the user added by hand.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.db import reset_devagent_engine, reset_engine  # noqa: E402

SLUG = "ua_employee"


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
def employee(platform):
    """An ordinary employee plus the bundled executor in the same store."""
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        create_orchestrator,
    )

    assert create_orchestrator(SLUG, "Plain Employee", "For tests") is not None
    assert create_orchestrator(UNIVERSAL_AGENT_SLUG, "Universal-Agent",
                               "For tests") is not None
    return SLUG


# ─── the shipped bundle ────────────────────────────────────────────────────


def test_bundled_folder_is_readable():
    """The bundle is what a fresh installation seeds: a folder that loads."""
    from core.defaults import load_default_orchestrator

    data = load_default_orchestrator("universal_agent")
    assert data is not None
    # The shipped bundle carries translations only: the user-defined slot is
    # empty and the label comes from the English translation.
    assert data["name_en"] == "Universal Agent"
    assert data["name_ru"] and data["name_zh"]
    assert data["display_name"]
    assert data["prompt_text"].strip()
    assert data["tools"], "an executor without tools cannot execute anything"


def test_bundled_prompt_is_english():
    """The prompt ships in one language: the bundled employees are English."""
    from core.defaults import load_default_orchestrator

    text = load_default_orchestrator("universal_agent")["prompt_text"]
    assert not any("\u0400" <= ch <= "\u04ff" for ch in text)


def test_bundled_toolset_stays_narrower_than_a_specialist():
    """The executor executes; it does not manage the platform's agents."""
    from core.defaults import load_default_orchestrator

    tools = set(load_default_orchestrator("universal_agent")["tools"])
    for forbidden in ("create_orchestrator", "delete_orchestrator",
                      "update_orchestrator", "create_assistant_for_task"):
        assert forbidden not in tools


# ─── the flag ──────────────────────────────────────────────────────────────


def test_absent_key_means_enabled(employee):
    """A config that never mentioned the flag must read as ON."""
    from core.orchestrators import get_orchestrator, get_universal_agent_enabled

    cfg = get_orchestrator(employee)["config"]
    assert "universal_agent_enabled" not in cfg
    assert get_universal_agent_enabled(employee) is True


def test_switching_off_is_stored_and_read_back(employee):
    from core.orchestrators import (
        get_orchestrator,
        get_universal_agent_enabled,
        set_universal_agent_enabled,
    )

    assert set_universal_agent_enabled(employee, False) is True
    assert get_orchestrator(employee)["config"]["universal_agent_enabled"] is False
    assert get_universal_agent_enabled(employee) is False


def test_switching_back_on_restores_the_right(employee):
    from core.orchestrators import get_universal_agent_enabled, set_universal_agent_enabled

    set_universal_agent_enabled(employee, False)
    set_universal_agent_enabled(employee, True)
    assert get_universal_agent_enabled(employee) is True


def test_flag_does_not_touch_the_allowlist(employee):
    """The two rights are stored separately: one never rewrites the other."""
    from core.orchestrators import (
        get_allowed_agents,
        set_allowed_agents,
        set_universal_agent_enabled,
    )

    set_allowed_agents(employee, ["research_agent"])
    set_universal_agent_enabled(employee, False)
    set_universal_agent_enabled(employee, True)
    assert get_allowed_agents(employee) == ["research_agent"]


def test_setter_refuses_an_unknown_orchestrator(platform):
    from core.orchestrators import set_universal_agent_enabled

    assert set_universal_agent_enabled("no_such_employee", False) is False


# ─── the effective list ────────────────────────────────────────────────────


def test_effective_list_adds_the_executor_by_default(employee):
    from core.orchestrators import UNIVERSAL_AGENT_SLUG, get_effective_agents

    assert get_effective_agents(employee) == [UNIVERSAL_AGENT_SLUG]


def test_effective_list_drops_it_after_switching_off(employee):
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        get_effective_agents,
        set_universal_agent_enabled,
    )

    set_universal_agent_enabled(employee, False)
    assert UNIVERSAL_AGENT_SLUG not in get_effective_agents(employee)


def test_effective_list_keeps_configured_agents_first(employee):
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        get_effective_agents,
        set_allowed_agents,
    )

    set_allowed_agents(employee, ["research_agent"])
    assert get_effective_agents(employee) == ["research_agent", UNIVERSAL_AGENT_SLUG]


def test_effective_list_never_duplicates_a_manual_entry(employee):
    """A user who added the slug by hand gets one entry, not two."""
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        get_effective_agents,
        set_allowed_agents,
    )

    set_allowed_agents(employee, [UNIVERSAL_AGENT_SLUG])
    assert get_effective_agents(employee) == [UNIVERSAL_AGENT_SLUG]


def test_effective_list_skips_itself(employee):
    """The executor does not reach itself through this entry."""
    from core.orchestrators import UNIVERSAL_AGENT_SLUG, get_effective_agents

    assert UNIVERSAL_AGENT_SLUG not in get_effective_agents(UNIVERSAL_AGENT_SLUG)


def test_effective_list_hides_a_deleted_executor(employee):
    """A dead slug in the list would be a dead end for the model."""
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        delete_orchestrator,
        get_effective_agents,
    )

    assert delete_orchestrator(UNIVERSAL_AGENT_SLUG) is True
    assert UNIVERSAL_AGENT_SLUG not in get_effective_agents(employee)


def test_effective_list_keeps_a_manual_entry_after_deletion(employee):
    """A hand-added slug stays in the raw allowlist, as the user wrote it."""
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        delete_orchestrator,
        get_allowed_agents,
        set_allowed_agents,
    )

    set_allowed_agents(employee, [UNIVERSAL_AGENT_SLUG])
    delete_orchestrator(UNIVERSAL_AGENT_SLUG)
    assert get_allowed_agents(employee) == [UNIVERSAL_AGENT_SLUG]
