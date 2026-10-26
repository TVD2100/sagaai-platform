# -*- coding: utf-8 -*-
"""
Tests for the ``memory_dir`` setting and the deletion cascade.

The working-data folder is stored inside the orchestrator config, so the
resolver keeps one source of truth. Deleting an employee must archive its
personal memory instead of destroying it.
"""
import os

import pytest

import core.paths


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp directory so the DB and memory stay isolated."""
    monkeypatch.setattr(core.paths, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(core.paths, "DB_PATH", os.path.join(str(tmp_path), "sagaai.db"))
    yield tmp_path


@pytest.fixture()
def created(isolated_data_dir):
    """A fresh orchestrator that is removed again after the test."""
    from core import orchestrators as orch
    slug = "memo_role"
    assert orch.create_orchestrator(slug, "Memo Role") is not None
    yield slug
    try:
        orch.delete_orchestrator(slug)
    except Exception:
        pass


# ─── memory_dir through save_orchestrator ────────────────────────────────────


def test_memory_dir_is_stored_in_config(created):
    from core import orchestrators as orch
    assert orch.save_orchestrator(created, memory_dir="bases/custom") is True
    stored = orch.get_orchestrator(created)
    assert stored["config"]["memory_dir"] == "bases/custom"


def test_memory_dir_empty_value_removes_the_key(created):
    from core import orchestrators as orch
    assert orch.save_orchestrator(created, memory_dir="bases/custom") is True
    assert orch.save_orchestrator(created, memory_dir="") is True
    stored = orch.get_orchestrator(created)
    assert "memory_dir" not in stored["config"]


def test_memory_dir_keeps_other_config_keys(created):
    from core import orchestrators as orch
    assert orch.save_orchestrator(created, config={"temperature": 0.4}) is True
    assert orch.save_orchestrator(created, memory_dir="bases/x") is True
    cfg = orch.get_orchestrator(created)["config"]
    assert cfg["temperature"] == 0.4
    assert cfg["memory_dir"] == "bases/x"


def test_memory_dir_reaches_the_resolver(created, tmp_path):
    from core import orchestrator_folders as of
    from core import orchestrators as orch
    assert orch.save_orchestrator(created, memory_dir="bases/custom") is True
    record = orch.get_orchestrator(created)
    resolved = of.resolve_data_dir(
        created, str(tmp_path),
        prompt_text=record.get("prompt_text") or "",
        config=record.get("config") or {})
    assert resolved["source"] == "config"
    assert resolved["path"] == os.path.join(str(tmp_path), "bases", "custom")


def test_prompt_marker_beats_config(created, tmp_path):
    from core import orchestrator_folders as of
    from core import orchestrators as orch
    assert orch.save_orchestrator(
        created, memory_dir="bases/from_config",
        prompt_text="MEMORY_DIR: bases/from_prompt") is True
    record = orch.get_orchestrator(created)
    resolved = of.resolve_data_dir(
        created, str(tmp_path),
        prompt_text=record.get("prompt_text") or "",
        config=record.get("config") or {})
    assert resolved["source"] == "prompt"
    assert resolved["path"] == os.path.join(str(tmp_path), "bases", "from_prompt")


def test_resolver_reads_the_stored_record(created, tmp_path, monkeypatch):
    """The resolver must load prompt and config itself when none is passed."""
    from core import orchestrator_folders as of
    from core import orchestrators as orch
    assert orch.save_orchestrator(created, memory_dir="self_loaded") is True
    resolved = of.resolve_data_dir(created, str(tmp_path))
    assert resolved["source"] == "config"
    assert resolved["path"] == os.path.join(str(tmp_path), "self_loaded")


# ─── Deletion cascade ────────────────────────────────────────────────────────


def test_delete_archives_personal_memory(created):
    from core import orchestrators as orch
    from core import personal_memory as pm

    scope = pm.format_scope(pm.SCOPE_ORCHESTRATOR, created)
    pm.write_memory(scope, "## Notes\n- важная заметка сотрудника")
    assert os.path.exists(pm.memory_path(scope))

    assert orch.delete_orchestrator(created) is True

    assert not os.path.exists(pm.memory_path(scope))
    archive_root = os.path.join(pm.personal_dir(), ".archive")
    archived = []
    for _root, _dirs, files in os.walk(archive_root):
        archived.extend(files)
    assert any(name.endswith(".md") for name in archived)


def test_delete_without_memory_still_succeeds(created):
    from core import orchestrators as orch
    assert orch.delete_orchestrator(created) is True


def test_builtin_employee_cannot_be_deleted(isolated_data_dir):
    from core import orchestrators as orch
    assert orch.delete_orchestrator("dev_agent") is False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
