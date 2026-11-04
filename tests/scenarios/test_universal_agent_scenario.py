# -*- coding: utf-8 -*-
"""tests/scenarios/test_universal_agent_scenario.py - the executor end to end.

Given a fresh installation that seeded the bundled employees
When a newly created employee delegates to the Universal-Agent
Then the spawn is allowed without any configuration step, and it stops
being allowed only after the user clears the checkbox.

This is the whole promise of the feature walked through the real entry
points: the default import, the runtime check and the prompt block.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from storage.db import reset_devagent_engine, reset_engine  # noqa: E402


@pytest.fixture
def platform(tmp_path, monkeypatch):
    """A fresh data dir; the bundled defaults/ folder stays the real one."""
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


# ─── given: a fresh installation ───────────────────────────────────────────


def test_fresh_install_seeds_the_executor(platform):
    """The bundled folder is what makes it preinstalled."""
    from core.default_imports import ensure_default_orchestrators
    from core.orchestrators import UNIVERSAL_AGENT_SLUG, get_orchestrator

    status = ensure_default_orchestrators()

    assert status.get(UNIVERSAL_AGENT_SLUG) == "created"
    seeded = get_orchestrator(UNIVERSAL_AGENT_SLUG)
    assert seeded is not None
    assert seeded["name"] == "Universal-Agent"


def test_seeded_executor_carries_its_prompt_and_tools(platform):
    from core.default_imports import ensure_default_orchestrators
    from core.orchestrators import UNIVERSAL_AGENT_SLUG
    from storage.repository import repo_get_orchestrator_with_text

    ensure_default_orchestrators()
    seeded = repo_get_orchestrator_with_text(UNIVERSAL_AGENT_SLUG)

    assert "Universal-Agent" in seeded["prompt_text"]
    assert "read_file" in seeded["tools"]


def test_second_boot_does_not_duplicate_it(platform):
    from core.default_imports import ensure_default_orchestrators
    from core.orchestrators import UNIVERSAL_AGENT_SLUG, list_orchestrators

    ensure_default_orchestrators()
    status = ensure_default_orchestrators()

    assert status.get(UNIVERSAL_AGENT_SLUG) == "exists"
    copies = [o for o in list_orchestrators() if o["slug"] == UNIVERSAL_AGENT_SLUG]
    assert len(copies) == 1


# ─── when: a newly created employee delegates ──────────────────────────────


def test_new_employee_can_spawn_it_without_configuration(platform):
    """The point of the default right: no setup step for a new employee."""
    from core.default_imports import ensure_default_orchestrators
    from core.orchestration.tools import check_agent_allowed
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        create_orchestrator,
        get_allowed_agents,
    )

    ensure_default_orchestrators()
    assert create_orchestrator("fresh_employee", "Fresh Employee", "For tests")

    # The stored allowlist stays empty - the right is a default, not an entry.
    assert get_allowed_agents("fresh_employee") == []
    assert check_agent_allowed("fresh_employee", UNIVERSAL_AGENT_SLUG) is None


def test_new_employee_prompt_advertises_it(platform):
    from core.default_imports import ensure_default_orchestrators
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        _extend_prompt_with_delegation,
        create_orchestrator,
    )

    ensure_default_orchestrators()
    create_orchestrator("fresh_employee", "Fresh Employee", "For tests")

    prompt = _extend_prompt_with_delegation("Base.", "fresh_employee")
    assert UNIVERSAL_AGENT_SLUG in prompt


# ─── then: the checkbox decides ────────────────────────────────────────────


def test_clearing_the_box_stops_the_spawn_and_restoring_returns_it(platform):
    from core.default_imports import ensure_default_orchestrators
    from core.orchestration.tools import check_agent_allowed
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        create_orchestrator,
        set_universal_agent_enabled,
    )

    ensure_default_orchestrators()
    create_orchestrator("fresh_employee", "Fresh Employee", "For tests")

    set_universal_agent_enabled("fresh_employee", False)
    refusal = check_agent_allowed("fresh_employee", UNIVERSAL_AGENT_SLUG)
    assert refusal is not None
    assert refusal.get("ok") is False

    set_universal_agent_enabled("fresh_employee", True)
    assert check_agent_allowed("fresh_employee", UNIVERSAL_AGENT_SLUG) is None


def test_the_decision_is_per_employee(platform):
    """One employee switching the executor off never touches another."""
    from core.default_imports import ensure_default_orchestrators
    from core.orchestration.tools import check_agent_allowed
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        create_orchestrator,
        set_universal_agent_enabled,
    )

    ensure_default_orchestrators()
    create_orchestrator("emp_a", "Employee A", "For tests")
    create_orchestrator("emp_b", "Employee B", "For tests")

    set_universal_agent_enabled("emp_a", False)

    assert check_agent_allowed("emp_a", UNIVERSAL_AGENT_SLUG) is not None
    assert check_agent_allowed("emp_b", UNIVERSAL_AGENT_SLUG) is None


def test_a_deleted_executor_leaves_no_dead_entry(platform):
    """The user may delete it; the right must then vanish, not dangle."""
    from core.default_imports import ensure_default_orchestrators
    from core.orchestration.tools import check_agent_allowed
    from core.orchestrators import (
        UNIVERSAL_AGENT_SLUG,
        create_orchestrator,
        delete_orchestrator,
    )

    ensure_default_orchestrators()
    create_orchestrator("fresh_employee", "Fresh Employee", "For tests")
    assert delete_orchestrator(UNIVERSAL_AGENT_SLUG) is True

    assert check_agent_allowed("fresh_employee", UNIVERSAL_AGENT_SLUG) is not None
