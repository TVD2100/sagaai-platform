# -*- coding: utf-8 -*-
"""
tests/scenarios/test_working_memory_scenario.py - end-to-end scenarios for the
working-data folder of an employee.

Every scenario walks the product the way a user does: through the agent
dispatcher (the only path an employee has to the file tools), through the
prompt the model actually receives, and through the migration entry point the
owner runs after an upgrade. Internal helpers appear only where the scenario
is about the file layout itself.

Scenario 1 (no project selected): the employee writes and reads its own
    registry before any project folder is picked; project paths stay blocked.
Scenario 2 (the prompt and the tools agree): the folder announced in the
    assembled prompt is exactly the folder the file tool resolves.
Scenario 3 (isolation): one employee cannot read a sibling's folder through
    either addressing form.
Scenario 4 (upgrade): legacy ``<project>/<slug>_data`` data is copied into the
    central folder, the employee reads it from there, and the legacy folder
    survives the migration.
"""
import os

import pytest

import core.paths
from core import memory_migration as mm
from core import orchestrators as orch


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Isolated DATA_DIR, database, central data root and project folder."""
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
def agent_factory(sandbox, monkeypatch):
    """Build a dispatcher for a fresh employee with NO project selected."""
    from dev_agent import config as dev_config
    from dev_agent.universal_agent import UniversalDevAgent

    monkeypatch.setattr(dev_config, "PROJECT_ROOT", sandbox["workspace"], raising=False)
    monkeypatch.setattr(dev_config, "WORKSPACE_SELECTED", False)

    created = []

    def _make(slug):
        assert orch.create_orchestrator(slug, slug.title()) is not None
        created.append(slug)
        agent = UniversalDevAgent()
        agent.attach_orchestrator(slug)
        return agent

    yield _make

    for slug in created:
        try:
            orch.delete_orchestrator(slug)
        except Exception:
            pass


# ─── Scenario 1: no project selected ─────────────────────────────────────────


def test_scenario_employee_keeps_its_registry_without_a_project(sandbox, agent_factory):
    # given: an employee bound to a dialog with no project folder chosen
    agent = agent_factory("scenario_desk")

    # when: it creates its registry and a unit card through the normal tools
    written = agent.dispatch("propose_file", {
        "path": "memory:registry.md",
        "content": "# Реестр\n\n| unit | status |\n|---|---|\n| a | in progress |\n",
    })
    card = agent.dispatch("propose_file", {
        "path": "memory:items/a/card.md",
        "content": "# Единица a\n- состояние: в работе\n",
    })

    # then: both writes land in the central folder, not in the project
    assert written.get("ok") is True, written
    assert card.get("ok") is True, card
    central = sandbox["central"] / "scenario_desk"
    assert (central / "registry.md").exists()
    assert (central / "items" / "a" / "card.md").exists()
    assert not (sandbox["workspace"] / "registry.md").exists()

    # and: the next dialog reads the registry back through the same prefix
    read = agent.dispatch("read_file", {"path": "memory:registry.md"})
    assert read.get("ok") is True, read
    assert "in progress" in read["content"]


def test_scenario_project_paths_stay_blocked_without_a_project(sandbox, agent_factory):
    # given: an employee with no project folder chosen
    agent = agent_factory("scenario_blocked")

    # when: it tries to read a project file
    result = agent.dispatch("read_file", {"path": "README.md"})

    # then: the workspace guard answers instead of reading anything
    assert result.get("ok") is False
    assert result.get("workspace_not_selected") is True


def test_scenario_listing_the_memory_folder_works_without_a_project(sandbox, agent_factory):
    # given: an employee with two records and no project folder
    agent = agent_factory("scenario_listing")
    agent.dispatch("propose_file", {"path": "memory:registry.md", "content": "# R\n"})
    agent.dispatch("propose_file", {"path": "memory:journal.md", "content": "# J\n"})

    # when: it lists its own folder
    listing = agent.dispatch("list_files", {"subdir": "memory:", "max_depth": 1})

    # then: the records are visible and addressed by the same prefix, so the
    # listing can be fed straight back into a file tool
    assert listing.get("ok") is True, listing
    paths = {f["path"] for f in listing.get("files", [])}
    assert {"memory:registry.md", "memory:journal.md"} <= paths


# ─── Scenario 2: the prompt and the tools agree ──────────────────────────────


def test_scenario_prompt_announces_the_same_folder_the_tools_use(sandbox, agent_factory):
    # given: a freshly created employee
    agent = agent_factory("scenario_prompt")

    # when: the prompt is assembled and a file is resolved
    from core.orchestrators import _extend_prompt_with_working_memory
    prompt = _extend_prompt_with_working_memory("# ROLE\nTest", "scenario_prompt", "")
    from dev_agent import config as dev_config
    announced = str(dev_config.memory_root())

    # then: the announced path is the one the prompt names and the tools reach
    assert announced == str(sandbox["central"] / "scenario_prompt")
    assert announced in prompt
    assert "memory:" in prompt


def test_scenario_folder_is_created_by_the_first_write(sandbox, agent_factory):
    # given: an employee whose folder does not exist yet
    agent = agent_factory("scenario_fresh")
    assert not (sandbox["central"] / "scenario_fresh").exists()

    # when: the first record is written
    result = agent.dispatch("propose_file", {
        "path": "memory:AGENT.md", "content": "# Данные\n- registry.md\n",
    })

    # then: the write succeeds and the folder now exists
    assert result.get("ok") is True, result
    assert (sandbox["central"] / "scenario_fresh" / "AGENT.md").exists()


# ─── Scenario 3: isolation between employees ─────────────────────────────────


def test_scenario_one_employee_cannot_read_another_folder(sandbox, agent_factory):
    # given: two employees, one of which has a private record
    writer = agent_factory("scenario_alpha")
    reader = agent_factory("scenario_beta")
    assert writer.dispatch("propose_file", {
        "path": "memory:secret.md", "content": "# альфа\n",
    }).get("ok") is True
    private = sandbox["central"] / "scenario_alpha" / "secret.md"

    # when: the second employee tries both addressing forms
    by_prefix = reader.dispatch("read_file", {"path": "memory:../scenario_alpha/secret.md"})
    by_absolute = reader.dispatch("read_file", {"path": str(private)})

    # then: neither reaches the sibling's data
    assert by_prefix.get("ok") is False
    assert by_absolute.get("ok") is False


def test_scenario_a_path_outside_both_roots_is_refused(sandbox, agent_factory, tmp_path,
                                                      monkeypatch):
    # given: an employee working IN a project, asked for a file outside both
    # allowed roots - the path check itself must refuse it, not the empty-state
    # guard
    from dev_agent import config as dev_config
    monkeypatch.setattr(dev_config, "WORKSPACE_SELECTED", True)
    agent = agent_factory("scenario_outside")
    outside = tmp_path / "elsewhere" / "note.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("x", encoding="utf-8")

    # when: it asks for that file
    result = agent.dispatch("read_file", {"path": str(outside)})

    # then: the read is refused
    assert result.get("ok") is False
    assert "escapes" in str(result.get("error")).lower()


# ─── Scenario 4: upgrade from the legacy layout ──────────────────────────────


def test_scenario_legacy_folder_is_migrated_and_still_readable(sandbox, agent_factory):
    # given: an employee whose data still sits in the old project folder
    agent = agent_factory("scenario_legacy")
    legacy = sandbox["workspace"] / "scenario_legacy_data"
    legacy.mkdir()
    (legacy / "registry.md").write_text("# Старый реестр\n", encoding="utf-8")

    # when: the owner runs the migration
    plan = mm.plan_migrations(str(sandbox["workspace"]), slugs=["scenario_legacy"])
    assert len(plan) == 1
    assert plan[0]["status"] == mm.STATUS_READY
    result = mm.apply_plan(plan)

    # then: the data is in the central folder and the employee reads it there
    assert result["ok"] is True, result
    read = agent.dispatch("read_file", {"path": "memory:registry.md"})
    assert read.get("ok") is True, read
    assert "Старый реестр" in read["content"]
    # and: nothing was deleted behind the owner's back
    assert (legacy / "registry.md").exists()


def test_scenario_migration_is_a_dry_run_without_apply(sandbox, agent_factory):
    # given: a legacy folder and an owner who only asks for the plan
    agent_factory("scenario_dry")
    legacy = sandbox["workspace"] / "scenario_dry_data"
    legacy.mkdir()
    (legacy / "registry.md").write_text("# R\n", encoding="utf-8")

    # when: the plan is applied in dry-run mode
    plan = mm.plan_migrations(str(sandbox["workspace"]), slugs=["scenario_dry"])
    result = mm.apply_plan(plan, dry_run=True)

    # then: nothing was copied
    assert result["ok"] is True
    assert result["total_copied"] == 0
    assert not (sandbox["central"] / "scenario_dry" / "registry.md").exists()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
