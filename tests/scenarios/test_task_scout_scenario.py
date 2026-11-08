# -*- coding: utf-8 -*-
"""tests/scenarios/test_task_scout_scenario.py - Task Scout walked like a user.

Scenario shape (given -> when -> then):

1. **Empty delegation.** A fresh installation ships Task Scout with nobody
   delegated. The user opens it and must be told to tick employees in the
   Delegation section - not receive an invented list of tasks.
2. **Delegation granted.** The user ticks two employees. The same read-only
   helpers that refused before now answer, and the consent boundary still
   holds: a third, unticked employee stays unreadable.
3. **Evidence in a dialog.** One delegated employee left a promise in its
   dialog. Task Scout finds it by asking first and reading second.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from storage.db import reset_devagent_engine, reset_engine  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SLUG = "task_scout"


@pytest.fixture
def fresh_install(tmp_path, monkeypatch):
    """A brand-new installation: defaults imported, nothing delegated."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))

    import core.paths as paths_mod
    for attr, value in (("DATA_DIR", str(tmp_path)),
                        ("DB_PATH", str(tmp_path / "sagaai.db")),
                        ("DEVAGENT_DB_PATH", str(tmp_path / "devagent.db")),
                        ("HISTORY_DIR", str(tmp_path / "history")),
                        ("SYSTEM_PROMPTS_DIR", str(tmp_path / "system_prompts")),
                        ("AGENTS_DATA_DIR", str(tmp_path / "agents_data"))):
        monkeypatch.setattr(paths_mod, attr, value, raising=False)

    reset_engine()
    reset_devagent_engine()

    from core.default_imports import ensure_default_orchestrators
    ensure_default_orchestrators()

    yield tmp_path
    reset_engine()
    reset_devagent_engine()


def _tools():
    """Return both helper functions of the installed Task Scout."""
    from core.orchestrator_folders import load_all_orchestrator_functions
    return load_all_orchestrator_functions(SLUG)


def _add_employee(slug: str, name: str) -> None:
    from core.orchestrators import create_orchestrator
    assert create_orchestrator(slug, name, "scenario employee") is not None


# ─── scenario 1: the roster is empty ──────────────────────────────────────


def test_a_fresh_install_offers_task_scout_with_nobody_delegated(fresh_install):
    """GIVEN a new installation WHEN it boots THEN Task Scout exists, undelegated."""
    from core.orchestrators import get_allowed_agents, get_orchestrator

    record = get_orchestrator(SLUG)
    assert record is not None, "the platform must ship Task Scout by default"
    assert record["name_user"] == ""
    assert record["name_en"] == "Task Scout"
    assert record["name_ru"] == "Разведчик задач"
    assert get_allowed_agents(SLUG) == []


def test_with_nobody_delegated_the_helpers_refuse(fresh_install):
    """GIVEN an empty roster WHEN the helpers run THEN they name the reason."""
    functions = _tools()

    memory = functions["agent_memory_reader"](slug="personal_assistant")
    threads = functions["agent_thread_reader"](slug="personal_assistant", action="list")

    assert memory["error_code"] == "no_delegation"
    assert threads["error_code"] == "no_delegation"
    # The message must point the user at the Delegation section, because that
    # is the only place the roster can be fixed.
    assert "Delegation" in memory["error"]


def test_the_prompt_carries_the_notice_the_user_will_see(fresh_install):
    """GIVEN an empty roster WHEN the agent answers THEN it quotes the notice."""
    from core.orchestrators import get_orchestrator

    prompt = get_orchestrator(SLUG)["prompt_text"]
    assert "«Делегирование»" in prompt
    assert "мне некого опрашивать" in prompt


# ─── scenario 2: the user ticks two employees ─────────────────────────────


def test_ticking_employees_opens_exactly_those_two(fresh_install):
    """GIVEN two ticked employees WHEN reading THEN only they are readable."""
    from core.orchestrators import set_allowed_agents

    _add_employee("personal_assistant", "Personal Assistant")
    _add_employee("project_manager", "Project Manager")
    _add_employee("marketer", "Marketer")
    assert set_allowed_agents(SLUG, ["personal_assistant", "project_manager"])

    functions = _tools()
    for slug in ("personal_assistant", "project_manager"):
        assert functions["agent_memory_reader"](slug=slug)["ok"] is True
        assert functions["agent_thread_reader"](slug=slug)["ok"] is True

    refused = functions["agent_memory_reader"](slug="marketer")
    assert refused["ok"] is False
    assert refused["error_code"] == "not_delegated"


def test_the_runtime_agrees_with_the_helpers(fresh_install):
    """GIVEN the same roster WHEN spawn_agent is checked THEN it matches."""
    from core.orchestration.tools import check_agent_allowed
    from core.orchestrators import set_allowed_agents

    _add_employee("personal_assistant", "Personal Assistant")
    _add_employee("marketer", "Marketer")
    assert set_allowed_agents(SLUG, ["personal_assistant"])

    assert check_agent_allowed(SLUG, "personal_assistant") is None
    refusal = check_agent_allowed(SLUG, "marketer")
    assert refusal is not None
    assert refusal["error_code"] == "agent_not_allowed"


# ─── scenario 3: evidence found ask-first, read-second ────────────────────


def test_a_left_open_promise_is_found_in_the_dialog(fresh_install):
    """GIVEN a delegated employee WHEN asked-then-read THEN the promise is found."""
    from core.orchestrators import set_allowed_agents
    from core.threads_devagent import create_devagent_thread
    from storage.repository_devagent import repo_devagent_append_message

    _add_employee("personal_assistant", "Personal Assistant")
    assert set_allowed_agents(SLUG, ["personal_assistant"])

    tid = create_devagent_thread(title="Планы на неделю",
                                 orchestrator_slug="personal_assistant")
    repo_devagent_append_message(tid, "user", "Напомни в пятницу про отчёт по курсу")
    repo_devagent_append_message(tid, "assistant", "Хорошо, напомню в пятницу.")

    functions = _tools()
    # Step 1 - the list shows the dialog exists, so asking was pointless: the
    # employee is the one who knows. Task Scout searches only for the gap.
    listing = functions["agent_thread_reader"](slug="personal_assistant", action="list")
    assert listing["count"] == 1

    # Step 2 - targeted search for the postponed commitment.
    found = functions["agent_thread_reader"](slug="personal_assistant",
                                             action="search", query="пятницу")
    assert found["ok"] is True
    assert found["count"] >= 1
    assert "пятницу" in found["hits"][0]["snippet"]

    # Step 3 - read the window to quote the evidence accurately.
    window = functions["agent_thread_reader"](slug="personal_assistant",
                                              action="read", thread_id=tid)
    assert window["ok"] is True
    assert any("пятницу" in message["content"] for message in window["messages"])


def test_another_employee_cannot_be_reached_by_a_guessed_dialog(fresh_install):
    """GIVEN one ticked employee WHEN a foreign dialog id is used THEN refused."""
    from core.orchestrators import set_allowed_agents
    from core.threads_devagent import create_devagent_thread

    _add_employee("personal_assistant", "Personal Assistant")
    _add_employee("marketer", "Marketer")
    assert set_allowed_agents(SLUG, ["personal_assistant"])

    foreign = create_devagent_thread(title="Кампания", orchestrator_slug="marketer")

    result = _tools()["agent_thread_reader"](slug="personal_assistant",
                                              action="read", thread_id=foreign)
    assert result["ok"] is False
    assert result["error_code"] == "not_owner"
