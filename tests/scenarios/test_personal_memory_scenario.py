# -*- coding: utf-8 -*-
"""
tests/scenarios/test_personal_memory_scenario.py - end-to-end scenarios for
the two-store memory model.

Every scenario walks the product the way a user does: through the agent
dispatcher (the only way an employee reaches the layer), through the prompt
assembly the model actually receives, and through the working-data-folder
resolver the settings page uses. Internal helpers are called only where the
scenario is about the file layout itself.

Scenario 1 (happy path): an employee keeps a durable note about how the owner
    wants the work done; a later dialog reads it back, the owner profile card
    and the employee block both reach the assembled prompt, and the note never
    leaks into the prompt text.
Scenario 2 (append vs replace): repeated notes accumulate, a replace rewrites
    the scope, and every write leaves a recoverable previous version.
Scenario 3 (limit and secrets): a note above the area limit and a note that
    looks like a credential are both refused, and the refusal never echoes the
    secret.
Scenario 4 (working folder): the ``MEMORY_DIR:`` marker wins over the config,
    an existing user folder produces no conflict, and a would-be relocation
    reports the data left behind instead of moving it silently.
Scenario 5 (isolation between stores): the owner scope, the employee scope and
    the project scope stay in separate files; one employee cannot write
    another employee's scope, and the project scope follows the workspace.
Scenario 6 (deletion): removing an employee archives its personal memory rather
    than destroying it, so the notes survive the person.
Scenario 7 (fresh install): an employee with no notes yet still receives the
    memory rules in its prompt, so the practice can start at all; the shared
    owner profile can be extended by any employee but not silently rewritten.
"""
import os

import pytest

import core.paths
from core import orchestrator_folders as of
from core import orchestrators as orch
from core import personal_memory as pm


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Isolated DATA_DIR, database and workspace for one scenario."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(core.paths, "DATA_DIR", str(data))
    monkeypatch.setattr(core.paths, "DB_PATH", os.path.join(str(data), "sagaai.db"))
    workspace = tmp_path / "project"
    workspace.mkdir()
    return {"data": data, "workspace": workspace}


@pytest.fixture()
def agent_factory(sandbox, monkeypatch):
    """Build a dispatcher bound to a freshly created employee."""
    from dev_agent import config as dev_config
    from dev_agent.universal_agent import UniversalDevAgent

    monkeypatch.setattr(dev_config, "PROJECT_ROOT", sandbox["workspace"], raising=False)
    monkeypatch.setattr(dev_config, "WORKSPACE_SELECTED", True, raising=False)

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


# ─── Scenario 1: happy path ──────────────────────────────────────────────────


def test_scenario_note_survives_the_dialog_and_reaches_the_prompt(sandbox, agent_factory):
    # given: an owner who fixed the report format and an employee who keeps
    # its own working note
    agent = agent_factory("scenario_writer")
    assert agent.dispatch("set_personal_memory", {
        "scope": "user",
        "mode": "append",
        "content": "## Preferences\n- отчёты: сначала вывод, затем детали",
    })["ok"] is True
    saved = agent.dispatch("set_personal_memory", {
        "content": "## Notes\n- рабочий тон: нейтральный",
    })
    assert saved["ok"] is True, saved

    # when: the next dialog starts and the prompt is assembled
    restored = agent.dispatch("get_personal_memory", {})
    strong, _weak = orch.build_assistant_dicts("scenario_writer")
    prompt = strong["text"]

    # then: the note is readable, the prompt advertises the store and the
    # owner card carries the preference - but the employee note text is never
    # inlined into the prompt, only its size is reported
    assert "нейтральный" in restored["content"]
    assert "## Owner profile" in prompt
    assert "сначала вывод" in prompt.split("## Memory", 1)[0]
    assert "## Memory" in prompt
    assert "orchestrator:scenario_writer" in prompt
    assert "get_personal_memory" in prompt
    assert "нейтральный" not in prompt


# ─── Scenario 2: append vs replace ──────────────────────────────────────────


def test_scenario_appends_accumulate_and_backups_allow_recovery(sandbox, agent_factory):
    # given: a first note that is extended twice
    agent = agent_factory("scenario_notes")
    assert agent.dispatch("set_personal_memory",
                          {"content": "## Notes\n- первая"})["ok"] is True
    assert agent.dispatch("set_personal_memory",
                          {"content": "- вторая", "mode": "append"})["ok"] is True
    assert agent.dispatch("set_personal_memory",
                          {"content": "- третья", "mode": "append"})["ok"] is True

    # when: the scope is read and then fully replaced
    grown = agent.dispatch("get_personal_memory", {})
    assert agent.dispatch("set_personal_memory",
                          {"content": "## Notes\n- только новая"})["ok"] is True
    replaced = agent.dispatch("get_personal_memory", {})

    # then: appending kept every note, replacing dropped them, and the earlier
    # versions are still on disk for recovery
    assert all(mark in grown["content"] for mark in ("первая", "вторая", "третья"))
    assert "только новая" in replaced["content"]
    assert "первая" not in replaced["content"]
    assert replaced["revision"] > grown["revision"]

    scope_path = pm.memory_path("orchestrator:scenario_notes")
    backup_dir = os.path.join(pm.personal_dir(), ".backups",
                              "orchestrator__scenario_notes")
    assert os.path.exists(scope_path)
    backups = [n for n in os.listdir(backup_dir) if n.endswith(".md")] \
        if os.path.isdir(backup_dir) else []
    assert backups, "previous versions must be recoverable"
    assert len(backups) <= pm.PERSONAL_BACKUPS_KEPT


# ─── Scenario 3: limits and secrets ─────────────────────────────────────────


def test_scenario_oversized_note_is_refused_with_the_limit(sandbox, agent_factory):
    # given: an employee whose note exceeds the area limit
    agent = agent_factory("scenario_limit")
    oversized = "## Notes\n" + ("- длинная строка заметки\n" * 3000)

    # when: the note is stored
    result = agent.dispatch("set_personal_memory", {"content": oversized})

    # then: the write is refused, the limit is named and nothing was created
    assert result["ok"] is False
    assert str(pm.PERSONAL_MEMORY_MAX_BYTES) in result["error"]
    assert pm.read_memory("orchestrator:scenario_limit")["exists"] is False


def test_scenario_credential_like_note_is_refused_without_echo(sandbox, agent_factory):
    # given: a note that accidentally contains a token
    agent = agent_factory("scenario_secret")
    secret = "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8"

    # when: the employee tries to remember it
    result = agent.dispatch("set_personal_memory",
                            {"content": "## Notes\n- токен " + secret})

    # then: the write is refused and the secret is not echoed back
    assert result["ok"] is False
    assert "credential" in result["error"].lower()
    assert secret not in result["error"]
    assert pm.read_memory("orchestrator:scenario_secret")["exists"] is False


# ─── Scenario 4: working folder ─────────────────────────────────────────────


def test_scenario_marker_wins_and_relocation_reports_old_data(sandbox):
    # given: an employee with data in its historical default folder
    workspace = sandbox["workspace"]
    legacy = workspace / "scenario_folder_data"
    legacy.mkdir()
    (legacy / "registry.md").write_text("# registry\n", encoding="utf-8")

    # when: the prompt points at a new place and the settings page resolves it
    resolved = of.resolve_data_dir(
        "scenario_folder", str(workspace),
        prompt_text="MEMORY_DIR: bases/new_home",
        config={"memory_dir": "bases/from_config"})

    # then: the marker wins, the path is inside the workspace and the data left
    # in the old folder is reported instead of being moved
    assert resolved["source"] == "prompt"
    assert resolved["path"] == os.path.join(str(workspace), "bases", "new_home")
    assert resolved["outside_workspace"] is False
    assert len(resolved["conflicts"]) == 1
    assert resolved["conflicts"][0]["files"] == 1
    assert (legacy / "registry.md").exists()
    assert not os.path.exists(resolved["path"])


# ─── Scenario 5: the three stores stay separate ─────────────────────────────


def test_scenario_stores_are_isolated_and_permissions_hold(sandbox, agent_factory):
    # given: two employees and one shared owner
    writer = agent_factory("scenario_alpha")
    reader = agent_factory("scenario_beta")

    # when: each writes its own scope and one tries to write the other's
    assert writer.dispatch("set_personal_memory",
                           {"content": "## Notes\n- альфа"})["ok"] is True
    assert reader.dispatch("set_personal_memory",
                           {"content": "## Notes\n- бета"})["ok"] is True
    assert writer.dispatch("set_personal_memory",
                           {"scope": "user", "mode": "append",
                            "content": "## Rules\n- правило"})["ok"] is True
    stolen = writer.dispatch("set_personal_memory",
                             {"scope": "orchestrator:scenario_beta", "content": "x"})

    # then: three separate files, no cross-employee write, and one listing
    alpha = pm.read_memory("orchestrator:scenario_alpha")
    beta = pm.read_memory("orchestrator:scenario_beta")
    assert "альфа" in alpha["content"] and "бета" not in alpha["content"]
    assert "бета" in beta["content"] and "альфа" not in beta["content"]
    assert stolen["ok"] is False
    assert "another employee" in stolen["error"]
    listed = {item["scope"] for item in writer.dispatch("list_personal_memory", {})["scopes"]}
    assert {"user", "orchestrator:scenario_alpha", "orchestrator:scenario_beta"} <= listed


def test_scenario_project_scope_follows_the_workspace(sandbox, agent_factory):
    # given: an employee working inside one project folder
    agent = agent_factory("scenario_project")

    # when: project-level notes are kept
    saved = agent.dispatch("set_personal_memory",
                           {"scope": "project", "content": "## Notes\n- стек: Python"})

    # then: the notes land in a project-keyed scope, not in the employee scope
    assert saved["ok"] is True, saved
    assert saved["kind"] == "project"
    assert len(saved["scope_id"]) == 12
    assert "стек" in pm.read_memory(saved["scope"])["content"]
    assert pm.read_memory("orchestrator:scenario_project")["exists"] is False


# ─── Scenario 6: deletion keeps the notes ───────────────────────────────────


def test_scenario_deletion_archives_memory_instead_of_destroying(sandbox, agent_factory):
    # given: an employee with a durable note
    agent = agent_factory("scenario_leaver")
    assert agent.dispatch("set_personal_memory",
                          {"content": "## Notes\n- важная договорённость"})["ok"] is True
    scope_path = pm.memory_path("orchestrator:scenario_leaver")
    assert os.path.exists(scope_path)

    # when: the employee is deleted
    assert orch.delete_orchestrator("scenario_leaver") is True

    # then: the live scope is gone, but the note survives in the archive
    assert not os.path.exists(scope_path)
    archive_root = os.path.join(pm.personal_dir(), ".archive")
    archived = []
    for root, _dirs, files in os.walk(archive_root):
        for name in files:
            if name.endswith(".md"):
                with open(os.path.join(root, name), encoding="utf-8") as handle:
                    archived.append(handle.read())
    assert any("важная договорённость" in text for text in archived)
# ─── Scenario 7: fresh install starts the practice ──────────────────────────


def test_scenario_fresh_employee_learns_to_keep_notes(sandbox, agent_factory):
    # given: a brand-new employee with no notes and no owner profile yet
    agent = agent_factory("scenario_newcomer")
    assert pm.read_memory("orchestrator:scenario_newcomer")["exists"] is False
    assert pm.read_memory("user")["exists"] is False

    # when: the prompt for the first dialog is assembled
    strong, _weak = orch.build_assistant_dicts("scenario_newcomer")
    prompt = strong["text"]

    # then: the rules are there even though no note exists - otherwise the
    # employee never learns that keeping notes is part of the job
    assert "## Memory" in prompt
    assert "is empty" in prompt
    assert "set_personal_memory" in prompt
    assert "corrects your approach" in prompt
    # the owner card is absent because it holds nothing yet
    assert "## Owner profile" not in prompt

    # and the employee can act on the rule right away
    saved = agent.dispatch("set_personal_memory",
                           {"content": "## Notes\n- отчёты: сначала вывод"})
    assert saved["ok"] is True, saved
    refreshed, _weak = orch.build_assistant_dicts("scenario_newcomer")
    assert "is empty" not in refreshed["text"]
    assert "bytes of notes" in refreshed["text"]


def test_scenario_owner_profile_is_shared_and_guarded(sandbox, agent_factory):
    # given: one employee recorded a rule of the owner
    first = agent_factory("scenario_owner_a")
    second = agent_factory("scenario_owner_b")
    assert first.dispatch("set_personal_memory", {
        "scope": "user", "mode": "append",
        "content": "## Rules\n- обращаться по имени",
    })["ok"] is True

    # when: a second employee tries to rewrite the shared card wholesale
    refused = second.dispatch("set_personal_memory", {
        "scope": "user", "content": "## Rules\n- обращаться на «вы»",
    })

    # then: the rewrite is refused and the first rule survives
    assert refused["ok"] is False
    assert "append" in refused["error"]
    kept = second.dispatch("get_personal_memory", {"scope": "user"})
    assert "по имени" in kept["content"]

    # and an extension is allowed, so both rules coexist
    added = second.dispatch("set_personal_memory", {
        "scope": "user", "mode": "append",
        "content": "## Preferences\n- краткие отчёты",
    })
    assert added["ok"] is True, added
    merged = second.dispatch("get_personal_memory", {"scope": "user"})["content"]
    assert "по имени" in merged and "краткие отчёты" in merged
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
