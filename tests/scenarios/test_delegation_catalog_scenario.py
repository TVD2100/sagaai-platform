# -*- coding: utf-8 -*-
"""tests/scenarios/test_delegation_catalog_scenario.py - what the model sees.

A tool the model cannot see is a feature nobody can use, and a tool it sees
without a working right is a dead end. This scenario walks the settings
layer the way the dialog does: configure delegation, build the catalog the
prompt receives, and check that the rights shown to the model match the
rights the runtime enforces.

Given a stored orchestrator configuration
When the tool catalog and the delegation getters are read
Then the catalog and the rights agree with each other.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from storage.db import reset_devagent_engine, reset_engine  # noqa: E402


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
    """An orchestrator with its own slug."""
    from core.orchestrators import create_orchestrator, get_orchestrator

    slug = "scenario_employee"
    assert create_orchestrator(slug, "Scenario Employee", "For tests") is not None
    assert get_orchestrator(slug) is not None
    return slug


# ─── catalog presence ──────────────────────────────────────────────────────


def _make_assistant(name="Scenario Helper"):
    """Create a real assistant card: the allowlist keeps only live ids."""
    from core.assistants import create_assistant

    aid = create_assistant(name, "yandex", "yandexgpt", 0.2, "You help.")
    assert aid
    return aid


def test_spawn_agent_is_always_in_the_catalog(employee):
    """Test 5: an EMPTY allowlist still offers spawn_agent - self-delegation
    needs no configuration, so hiding the tool would hide the default case."""
    from core.orchestrator_tools import build_tool_catalog

    names = [t["name"] for t in build_tool_catalog(employee)]
    assert "spawn_agent" in names
    assert "invoke_assistant" not in names


def test_invoke_assistant_appears_once_an_assistant_is_allowed(employee):
    from core.orchestrators import set_allowed_assistants
    from core.orchestrator_tools import build_tool_catalog

    set_allowed_assistants(employee, [_make_assistant()])

    names = [t["name"] for t in build_tool_catalog(employee)]
    assert "invoke_assistant" in names


def test_assistant_allowlist_is_validated_against_live_cards(employee):
    """A stale id never survives the setter, so the catalog cannot offer a
    tool whose only listed assistant no longer exists."""
    from core.orchestrators import get_allowed_assistants, set_allowed_assistants
    from core.orchestrator_tools import build_tool_catalog

    set_allowed_assistants(employee, ["ghost999"])

    assert get_allowed_assistants(employee) == []
    names = [t["name"] for t in build_tool_catalog(employee)]
    assert "invoke_assistant" not in names


def test_catalog_descriptions_state_the_boundaries(employee):
    from core.orchestrators import set_allowed_assistants
    from core.orchestrator_tools import build_tool_catalog

    set_allowed_assistants(employee, [_make_assistant()])

    entries = {t["name"]: t["desc"] for t in build_tool_catalog(employee)}
    assert "CLEAN history" in entries["spawn_agent"]
    assert "always allowed" in entries["spawn_agent"]
    assert "no agent loop" in entries["invoke_assistant"]


def test_disabling_a_delegation_tool_removes_it_from_the_catalog(employee):
    """A disabled tool must not be advertised in the prompt block.

    The catalog is built with the caller's disabled set - the same set the
    prompt builder passes - so this pins the filtering contract itself.
    """
    from core.orchestrators import (get_disabled_tools, set_allowed_assistants,
                                    set_disabled_tools)
    from core.orchestrator_tools import build_tool_catalog

    set_allowed_assistants(employee, [_make_assistant()])
    set_disabled_tools(employee, ["spawn_agent", "invoke_assistant"])

    names = [t["name"] for t in
             build_tool_catalog(employee, disabled=set(get_disabled_tools(employee)))]
    assert "spawn_agent" not in names
    assert "invoke_assistant" not in names


def test_prompt_block_lists_the_delegation_tools(employee):
    from core.orchestrator_tools import render_available_tools_block

    block = render_available_tools_block(employee)
    assert "spawn_agent" in block


# ─── stored allowlists ─────────────────────────────────────────────────────


def test_self_slug_is_not_stored_in_the_allowlist(employee):
    """Self-delegation is structural: storing it would put a meaningless
    checkbox for yourself into the settings tab."""
    from core.orchestrators import get_allowed_agents, set_allowed_agents

    set_allowed_agents(employee, [employee, "other_agent", "other_agent", ""])

    assert get_allowed_agents(employee) == ["other_agent"]


def test_allowlist_survives_a_config_round_trip(employee):
    from core.orchestrators import (get_allowed_agents, get_delegation_limits,
                                    set_allowed_agents, set_delegation_limits)

    set_allowed_agents(employee, ["other_agent"])
    set_delegation_limits(employee, {"max_depth": 2, "total_subagent_runs": 8})

    assert get_allowed_agents(employee) == ["other_agent"]
    limits = get_delegation_limits(employee)
    assert limits["max_depth"] == 2
    assert limits["total_subagent_runs"] == 8
    # Untouched keys keep their documented default.
    assert limits["max_active_subagents"] == 3


def test_limits_are_clamped_on_write(employee):
    """A zero pool would deadlock the first spawn: the setter clamps it."""
    from core.orchestrators import get_delegation_limits, set_delegation_limits

    set_delegation_limits(employee, {"max_active_subagents": 0,
                                     "subagent_timeout_seconds": -1})

    limits = get_delegation_limits(employee)
    assert limits["max_active_subagents"] == 1
    assert limits["subagent_timeout_seconds"] == 600


def test_unknown_limit_keys_are_ignored(employee):
    from core.orchestrators import get_delegation_limits, set_delegation_limits

    set_delegation_limits(employee, {"max_depth": 3, "sneaky_key": 999})

    limits = get_delegation_limits(employee)
    assert limits["max_depth"] == 3
    assert "sneaky_key" not in limits


def test_settings_for_an_unknown_orchestrator_are_refused():
    from core.orchestrators import (set_allowed_agents, set_allowed_assistants,
                                    set_delegation_limits)

    assert set_allowed_agents("nobody_here", ["x"]) is False
    assert set_allowed_assistants("nobody_here", ["x"]) is False
    assert set_delegation_limits("nobody_here", {"max_depth": 2}) is False


# ─── rights and catalog agree ─────────────────────────────────────────────


def test_catalog_and_runtime_read_the_same_allowlist(employee):
    """The check the loop performs and the check the catalog is built from
    must not diverge - otherwise the model is offered a refused tool."""
    from core.orchestration.tools import check_agent_allowed
    from core.orchestrators import set_allowed_agents
    from core.orchestrator_tools import build_tool_catalog

    set_allowed_agents(employee, ["helper_agent"])

    assert "spawn_agent" in [t["name"] for t in build_tool_catalog(employee)]
    assert check_agent_allowed(employee, "helper_agent") is None
    assert check_agent_allowed(employee, employee) is None
    refusal = check_agent_allowed(employee, "stranger")
    assert refusal["error_code"] == "agent_not_allowed"
    assert set(refusal["allowed"]) == {employee, "helper_agent"}
