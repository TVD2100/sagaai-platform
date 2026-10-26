# -*- coding: utf-8 -*-
"""
Tests for the personal-memory agent tools exposed by the dispatcher.

The three tools (get/set/list) are the only way an employee reaches the
personal layer, so these tests focus on the tool contract: scope resolution
('current', 'project', explicit scopes), the replace/append modes, argument
validation and the access policy the memory layer enforces behind them.
"""
import os

import pytest

import core.paths
from core import personal_memory as pm


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp directory so memory never touches real data."""
    monkeypatch.setattr(core.paths, "DATA_DIR", str(tmp_path))
    yield tmp_path


@pytest.fixture()
def agent(isolated_data_dir, monkeypatch, tmp_path):
    """A dispatcher bound to an orchestrator and to a temporary workspace."""
    from dev_agent import config as dev_config
    from dev_agent.universal_agent import UniversalDevAgent

    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(dev_config, "PROJECT_ROOT", workspace, raising=False)
    monkeypatch.setattr(dev_config, "WORKSPACE_SELECTED", True, raising=False)

    instance = UniversalDevAgent()
    instance.attach_orchestrator("marketer")
    return instance


# ─── Catalog wiring ──────────────────────────────────────────────────────────


def test_tools_are_registered(agent):
    for name in ("get_personal_memory", "set_personal_memory", "list_personal_memory"):
        assert name in agent._extra, name


def test_tools_are_listed_in_catalog():
    from dev_agent.universal_agent import WORKSPACE_TOOL_CATALOG
    names = {entry["name"] for entry in WORKSPACE_TOOL_CATALOG}
    assert {"get_personal_memory", "set_personal_memory",
            "list_personal_memory"} <= names


# ─── Write / read round trip ─────────────────────────────────────────────────


def test_set_then_get_current_scope(agent):
    saved = agent.dispatch("set_personal_memory",
                           {"content": "## Notes\n- предпочитает краткие отчёты"})
    assert saved["ok"] is True, saved
    assert saved["scope"] == "orchestrator:marketer"

    read = agent.dispatch("get_personal_memory", {})
    assert read["ok"] is True, read
    assert "краткие отчёты" in read["content"]
    assert read["exists"] is True
    assert read["revision"] >= 1


def test_set_user_scope_is_allowed_with_append(agent):
    saved = agent.dispatch("set_personal_memory",
                           {"scope": "user", "mode": "append",
                            "content": "## Rules\n- обращаться по имени"})
    assert saved["ok"] is True, saved
    assert saved["scope"] == "user"
    assert os.path.exists(pm.memory_path("user"))


def test_bare_project_scope_targets_active_workspace(agent):
    saved = agent.dispatch("set_personal_memory",
                           {"scope": "project", "content": "## Notes\n- стек: Python"})
    assert saved["ok"] is True, saved
    assert saved["kind"] == "project"
    assert saved["scope_id"]  # a 12-char key, not the raw path
    assert len(saved["scope_id"]) == 12


def test_append_mode_keeps_previous_text(agent):
    agent.dispatch("set_personal_memory", {"content": "## Notes\n- первая заметка"})
    appended = agent.dispatch("set_personal_memory",
                              {"content": "- вторая заметка", "mode": "append"})
    assert appended["ok"] is True, appended
    read = agent.dispatch("get_personal_memory", {})
    assert "первая заметка" in read["content"]
    assert "вторая заметка" in read["content"]


def test_owner_profile_rewrite_is_refused_by_default(agent):
    """The owner card is shared: a silent rewrite would drop other notes."""
    agent.dispatch("set_personal_memory", {"scope": "user", "mode": "append",
                                            "content": "## Rules\n- правило"})
    res = agent.dispatch("set_personal_memory",
                         {"scope": "user", "content": "## Rules\n- новое правило"})
    assert res["ok"] is False
    assert "append" in res["error"]
    kept = agent.dispatch("get_personal_memory", {"scope": "user"})
    assert "правило" in kept["content"]
    assert "новое правило" not in kept["content"]


def test_owner_profile_rewrite_is_allowed_with_explicit_flag(agent):
    agent.dispatch("set_personal_memory", {"scope": "user", "mode": "append",
                                            "content": "## Rules\n- старое"})
    res = agent.dispatch("set_personal_memory",
                         {"scope": "user", "content": "## Rules\n- новое",
                          "overwrite_owner_profile": True})
    assert res["ok"] is True, res
    assert "новое" in agent.dispatch("get_personal_memory", {"scope": "user"})["content"]


def test_owner_profile_flag_accepts_a_string(agent):
    """A JSON caller may send 'true' as a string instead of a boolean."""
    res = agent.dispatch("set_personal_memory",
                         {"scope": "user", "content": "## Rules\n- новое",
                          "overwrite_owner_profile": "true"})
    assert res["ok"] is True, res


def test_default_get_scope_is_the_employees_own(agent):
    """The catalog says 'current' is the default - it must match dispatch."""
    agent.dispatch("set_personal_memory", {"content": "## Notes\n- своя заметка"})
    agent.dispatch("set_personal_memory", {"scope": "user", "mode": "append",
                                            "content": "## Rules\n- правило владельца"})
    read = agent.dispatch("get_personal_memory", {})
    assert read["scope"] == "orchestrator:marketer"
    assert "своя заметка" in read["content"]
    assert "правило владельца" not in read["content"]


def test_replace_mode_drops_previous_text(agent):
    agent.dispatch("set_personal_memory", {"content": "## Notes\n- старая"})
    agent.dispatch("set_personal_memory", {"content": "## Notes\n- новая"})
    read = agent.dispatch("get_personal_memory", {})
    assert "новая" in read["content"]
    assert "старая" not in read["content"]


def test_max_chars_truncates_read(agent):
    agent.dispatch("set_personal_memory",
                   {"content": "## Notes\n" + "- длинная строка\n" * 40})
    short = agent.dispatch("get_personal_memory", {"max_chars": 20})
    assert short["ok"] is True
    assert len(short["content"]) == 20
    assert short["truncated"] is True


# ─── Access policy ───────────────────────────────────────────────────────────


def test_other_employee_scope_is_refused(agent):
    res = agent.dispatch("set_personal_memory",
                         {"scope": "orchestrator:dev_agent", "content": "чужая память"})
    assert res["ok"] is False
    assert "another employee" in res["error"]


def test_connector_scope_is_read_only_for_agent(agent, monkeypatch):
    from core import personal_memory
    monkeypatch.setattr(personal_memory, "_enabled_connection_ids", lambda slug: {"abc12345"})
    res = agent.dispatch("set_personal_memory",
                         {"scope": "connector:abc12345", "content": "x"})
    assert res["ok"] is False
    assert "connector tools" in res["error"].lower()


# ─── Validation ──────────────────────────────────────────────────────────────


def test_credential_like_content_is_refused(agent):
    res = agent.dispatch("set_personal_memory",
                         {"content": "token: sk-abcdefghijklmnopqrstuvwxyz012345"})
    assert res["ok"] is False
    assert "credential" in res["error"].lower()
    # The refusal must not echo the secret back.
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in res["error"]


def test_invalid_mode_is_refused(agent):
    res = agent.dispatch("set_personal_memory", {"content": "x", "mode": "merge"})
    assert res["ok"] is False
    assert "mode" in res["error"]


def test_unknown_scope_kind_is_refused(agent):
    res = agent.dispatch("get_personal_memory", {"scope": "galaxy:andromeda"})
    assert res["ok"] is False


def test_unknown_argument_is_rejected(agent):
    res = agent.dispatch("get_personal_memory", {"scope": "user", "bogus": 1})
    assert res["ok"] is False
    assert "unknown" in res["error"].lower() or "unexpected" in res["error"].lower()


# ─── Listing ─────────────────────────────────────────────────────────────────


def test_list_reports_written_scopes(agent):
    agent.dispatch("set_personal_memory", {"content": "## Notes\n- своя"})
    agent.dispatch("set_personal_memory", {"scope": "user", "mode": "append",
                                            "content": "## Notes\n- владелец"})
    listed = agent.dispatch("list_personal_memory", {})
    assert listed["ok"] is True, listed
    scopes = {item["scope"] for item in listed["scopes"]}
    assert "orchestrator:marketer" in scopes
    assert "user" in scopes


def test_list_is_empty_on_fresh_data_dir(agent):
    listed = agent.dispatch("list_personal_memory", {})
    assert listed["ok"] is True
    assert listed["count"] == 0
    assert listed["scopes"] == []
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
