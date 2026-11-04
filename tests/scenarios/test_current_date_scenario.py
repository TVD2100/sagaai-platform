# -*- coding: utf-8 -*-
"""
tests/scenarios/test_current_date_scenario.py - end-to-end scenarios for the
current-date support of an employee.

Every scenario walks the product the way a user does: through the prompt the
model actually receives, through the agent dispatcher (the only path an
employee has to its tools), and through the files a role keeps on disk.

Scenario 1 (a fresh employee knows the day): the assembled prompt states
    today's date before the role has read a single file.
Scenario 2 (prompt and tool agree): the date the prompt states is exactly the
    date the clock tool reports - one clock, no drift.
Scenario 3 (a stale cache loses): a role reads an out-of-date summary and then
    asks the clock; the two disagree, and the clock is the one that is right.
Scenario 4 (the block is optional, the tool is not): an employee that switched
    the prompt block off can still ask for the time.
Scenario 5 (the day changes only the tail): two prompts assembled on different
    days share their whole prefix, so the provider cache survives midnight.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

import core.paths
from core import orchestrators as orch

BLOCK_MARK = "\n\n## Current date\n\n"
DAY = timedelta(days=1)


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Isolated DATA_DIR, database and project folder."""
    data = tmp_path / "data"
    data.mkdir()
    central = data / "agents_data"
    central.mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(core.paths, "DATA_DIR", str(data))
    monkeypatch.setattr(core.paths, "DB_PATH", os.path.join(str(data), "sagaai.db"))
    monkeypatch.setattr(core.paths, "AGENTS_DATA_DIR", str(central))
    return {"data": data, "central": central, "workspace": workspace}


@pytest.fixture()
def employee(sandbox, monkeypatch):
    """Create a real employee and return its slug; clean it up afterwards."""
    created = []

    def _make(slug, config=None):
        record = orch.create_orchestrator(slug, slug.title(),
                                          prompt_text="# ROLE\nYou answer questions.",
                                          config=config or {})
        assert record is not None, "could not create the employee"
        created.append(slug)
        return slug

    yield _make

    for slug in created:
        try:
            orch.delete_orchestrator(slug)
        except Exception:
            pass


@pytest.fixture()
def agent(sandbox, monkeypatch):
    """Build a dispatcher bound to the scenario's project folder."""
    from dev_agent import config as dev_config
    from dev_agent.universal_agent import UniversalDevAgent

    monkeypatch.setattr(dev_config, "PROJECT_ROOT", sandbox["workspace"], raising=False)
    monkeypatch.setattr(dev_config, "WORKSPACE_SELECTED", True)
    return UniversalDevAgent()


# ─── Scenario 1: a fresh employee knows the day ─────────────────────────────


def test_scenario_a_fresh_employee_knows_today(sandbox, employee):
    # given: an employee that has read nothing yet
    slug = employee("scenario_fresh")

    # when: the prompt is assembled for the model
    strong, _weak = orch.build_assistant_dicts(slug, str(sandbox["workspace"]))
    prompt = strong["text"]

    # then: the date is already stated, in words, without asking any file
    assert BLOCK_MARK in prompt
    today = datetime.now()
    assert orch._current_date_line(today) in prompt
    assert prompt.rstrip().endswith(".")


def test_scenario_the_date_block_closes_the_prompt(sandbox, employee):
    # given: an employee with a role prompt and a tool catalog
    slug = employee("scenario_tail")

    # when: the prompt is assembled
    strong, _weak = orch.build_assistant_dicts(slug, str(sandbox["workspace"]))
    prompt = strong["text"]

    # then: nothing follows the date block, so midnight only invalidates the tail
    tail = prompt.split(BLOCK_MARK, 1)[1]
    assert "## " not in tail
    assert tail.count("Installation timezone:") == 1


# ─── Scenario 2: prompt and tool agree ──────────────────────────────────────


def test_scenario_the_prompt_and_the_clock_tool_agree(sandbox, employee, agent):
    # given: an employee whose prompt states the day
    slug = employee("scenario_agree")
    strong, _weak = orch.build_assistant_dicts(slug, str(sandbox["workspace"]))
    prompt = strong["text"]

    # when: the employee asks the clock tool
    answer = agent.dispatch("current_datetime", {})

    # then: both statements are the same sentence from the same clock
    assert answer.get("ok") is True, answer
    assert answer["date_line"] in prompt
    assert answer["date"] in answer["iso"] or answer["date_iso"] in answer["iso"]


def test_scenario_the_clock_tool_is_offered_in_the_prompt(sandbox, employee):
    # given: an employee
    slug = employee("scenario_offered")

    # when: the prompt is assembled
    strong, _weak = orch.build_assistant_dicts(slug, str(sandbox["workspace"]))

    # then: the tool is in the catalog the model reads, with its usage line
    assert "`current_datetime`" in strong["text"]


# ─── Scenario 3: a stale cache loses ────────────────────────────────────────


def test_scenario_a_stale_summary_does_not_set_the_date(sandbox, employee, agent,
                                                        monkeypatch):
    # given: a role that keeps a summary written three days ago
    slug = employee("scenario_stale")
    stale_day = datetime.now() - 3 * DAY
    summary_path = sandbox["workspace"] / "TODAY.md"
    summary_path.write_text(
        "# Сводка\nUpdated: %s\n\n- Занятий нет\n" % stale_day.strftime("%d.%m.%Y"),
        encoding="utf-8",
    )

    # when: the employee reads the summary and then asks the clock
    read = agent.dispatch("read_file", {"path": "TODAY.md"})
    clock = agent.dispatch("current_datetime", {})

    # then: the file states an older day, the clock states today, and the
    # platform's own statement is the one the prompt carries
    assert read.get("ok") is True, read
    assert stale_day.strftime("%d.%m.%Y") in read["content"]
    assert clock["date"] != stale_day.strftime("%d.%m.%Y")
    strong, _weak = orch.build_assistant_dicts(slug, str(sandbox["workspace"]))
    assert clock["date_line"] in strong["text"]


def test_scenario_the_date_never_comes_from_a_file(sandbox, employee, monkeypatch):
    # given: a project whose files all claim an old date
    slug = employee("scenario_nofile")
    (sandbox["workspace"] / "journal.md").write_text(
        "# Журнал\n01.01.2020 - начало\n", encoding="utf-8"
    )

    # when: the prompt is assembled
    strong, _weak = orch.build_assistant_dicts(slug, str(sandbox["workspace"]))
    prompt = strong["text"]

    # then: the file's date is absent and today's is present
    assert "01 January 2020" not in prompt
    assert orch._current_date_line(datetime.now()) in prompt


# ─── Scenario 4: the block is optional, the tool is not ─────────────────────


def test_scenario_a_role_may_switch_the_block_off_but_keep_the_clock(sandbox,
                                                                     employee,
                                                                     agent):
    # given: an employee that switched the prompt block off
    slug = employee("scenario_switch", config={"current_date_enabled": False})

    # when: the prompt is assembled and the clock is asked
    strong, _weak = orch.build_assistant_dicts(slug, str(sandbox["workspace"]))
    answer = agent.dispatch("current_datetime", {})

    # then: the prompt carries no date, but the tool still answers
    assert BLOCK_MARK not in strong["text"]
    assert answer.get("ok") is True, answer
    assert answer["date_iso"] == datetime.now().strftime("%Y-%m-%d")


def test_scenario_a_disabled_clock_tool_leaves_the_date_intact(sandbox, employee):
    # given: an employee whose clock tool is switched off
    slug = employee("scenario_notool", config={"disabled_tools": ["current_datetime"]})

    # when: the prompt is assembled
    strong, _weak = orch.build_assistant_dicts(slug, str(sandbox["workspace"]))

    # then: the tool is gone from the catalog, the date is still stated
    assert BLOCK_MARK in strong["text"]
    assert "`current_datetime`" not in strong["text"]


# ─── Scenario 5: the day changes only the tail ──────────────────────────────


def test_scenario_midnight_changes_only_the_tail_of_the_prompt(monkeypatch):
    # given: an employee assembled on one day and on the next
    monkeypatch.setattr(orch, "get_orchestrator", lambda slug: {
        "prompt_text": "# ROLE\nStable body",
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
    monkeypatch.setattr(orch, "_extend_prompt_with_tools",
                        lambda p, s: p + "\n\n## Available tools\n\n- fake_tool")
    monkeypatch.setattr(orch, "_get_services", lambda: {})

    first = datetime(2026, 11, 2, 23, 59, tzinfo=timezone(timedelta(hours=3)))
    second = first + DAY

    # when: both prompts are assembled
    today_prompt = orch._extend_prompt_with_current_date("# ROLE\nStable body",
                                                         "probe", now=first)
    tomorrow_prompt = orch._extend_prompt_with_current_date("# ROLE\nStable body",
                                                           "probe", now=second)

    # then: everything up to the date block is byte-identical
    prefix_today = today_prompt.split(BLOCK_MARK, 1)[0]
    prefix_tomorrow = tomorrow_prompt.split(BLOCK_MARK, 1)[0]
    assert prefix_today == prefix_tomorrow
    assert today_prompt != tomorrow_prompt
    assert orch._current_date_line(first) in today_prompt
    assert orch._current_date_line(second) in tomorrow_prompt
