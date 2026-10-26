# -*- coding: utf-8 -*-
"""
Tests for the working-data-folder resolver of an orchestrator.

Covers the ``MEMORY_DIR:`` prompt marker, the config fallback, the default
``<workspace>/<slug>_data`` path, path-escape protection and the conflict
report that protects existing data from silent relocation.
"""
import os

import pytest

from core import orchestrator_folders as of


# ─── Marker parsing ───────────────────────────────────────────────────────────


def test_marker_absent_returns_empty():
    assert of.parse_memory_dir_marker("## ROLE\nОбычный промпт без маркера.") == ""
    assert of.parse_memory_dir_marker("") == ""


def test_marker_relative_value():
    prompt = "## Память и данные\nMEMORY_DIR: project_manager_data\nдалее текст"
    assert of.parse_memory_dir_marker(prompt) == "project_manager_data"


def test_marker_is_case_insensitive_and_trimmed():
    prompt = "   memory_dir:   editor_assistant_data   "
    assert of.parse_memory_dir_marker(prompt) == "editor_assistant_data"


def test_marker_first_occurrence_wins():
    prompt = "MEMORY_DIR: first_dir\n...\nMEMORY_DIR: second_dir"
    assert of.parse_memory_dir_marker(prompt) == "first_dir"


def test_marker_inside_code_fence_is_ignored():
    prompt = "```markdown\nMEMORY_DIR: example_only\n```\nMEMORY_DIR: real_dir"
    assert of.parse_memory_dir_marker(prompt) == "real_dir"


def test_marker_backticks_are_stripped():
    assert of.parse_memory_dir_marker("MEMORY_DIR: `my_data`") == "my_data"


# ─── Resolution ───────────────────────────────────────────────────────────────


def test_default_path_uses_workspace_and_slug(tmp_path):
    result = of.resolve_data_dir("project_manager", str(tmp_path),
                                 prompt_text="no marker here", config={})
    assert result["source"] == "default"
    assert result["path"] == os.path.join(str(tmp_path), "project_manager_data")
    assert result["available"] is True
    assert result["exists"] is False


def test_relative_marker_resolves_against_workspace(tmp_path):
    result = of.resolve_data_dir("marketer", str(tmp_path),
                                 prompt_text="MEMORY_DIR: bases/mkt", config={})
    assert result["source"] == "prompt"
    assert result["path"] == os.path.join(str(tmp_path), "bases", "mkt")
    assert result["outside_workspace"] is False


def test_marker_dot_means_workspace_root(tmp_path):
    result = of.resolve_data_dir("editor_assistant", str(tmp_path),
                                 prompt_text="MEMORY_DIR: .", config={})
    assert result["source"] == "prompt"
    assert result["path"] == str(tmp_path)


def test_marker_default_falls_back_to_slug_folder(tmp_path):
    result = of.resolve_data_dir("teacher_assistant", str(tmp_path),
                                 prompt_text="MEMORY_DIR: default", config={})
    assert result["source"] == "default"
    assert result["path"] == os.path.join(str(tmp_path), "teacher_assistant_data")


def test_absolute_marker_outside_workspace_is_flagged(tmp_path):
    external = tmp_path.parent / "external_base"
    external.mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    result = of.resolve_data_dir("teacher_assistant", str(workspace),
                                 prompt_text=f"MEMORY_DIR: {external}", config={})
    assert result["source"] == "prompt"
    assert result["path"] == str(external)
    assert result["outside_workspace"] is True


def test_relative_marker_escaping_workspace_is_ignored(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    result = of.resolve_data_dir("marketer", str(workspace),
                                 prompt_text="MEMORY_DIR: ../../etc", config={})
    assert result["source"] == "default"
    assert result["path"] == os.path.join(str(workspace), "marketer_data")


def test_config_used_only_without_marker(tmp_path):
    config = {"memory_dir": "from_config"}
    with_config = of.resolve_data_dir("marketer", str(tmp_path),
                                      prompt_text="без маркера", config=config)
    assert with_config["source"] == "config"
    assert with_config["path"] == os.path.join(str(tmp_path), "from_config")

    with_marker = of.resolve_data_dir(
        "marketer", str(tmp_path),
        prompt_text="MEMORY_DIR: from_prompt", config=config)
    assert with_marker["source"] == "prompt"
    assert with_marker["path"] == os.path.join(str(tmp_path), "from_prompt")


def test_no_workspace_and_relative_marker_is_unavailable():
    result = of.resolve_data_dir("marketer", "",
                                 prompt_text="MEMORY_DIR: some_dir", config={})
    assert result["available"] is False
    assert result["path"] == ""


def test_no_workspace_but_absolute_marker_is_available(tmp_path):
    result = of.resolve_data_dir("marketer", "",
                                 prompt_text=f"MEMORY_DIR: {tmp_path}", config={})
    assert result["available"] is True
    assert result["path"] == str(tmp_path)


# ─── Conflict report ─────────────────────────────────────────────────────────


def test_existing_default_folder_is_reported_as_conflict(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    legacy = workspace / "editor_assistant_data"
    legacy.mkdir()
    (legacy / "registry.md").write_text("# реестр", encoding="utf-8")

    result = of.resolve_data_dir("editor_assistant", str(workspace),
                                 prompt_text="MEMORY_DIR: bases/editor", config={})
    assert result["source"] == "prompt"
    assert result["path"] == os.path.join(str(workspace), "bases", "editor")
    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["source"] == "default"
    assert conflict["path"] == str(legacy)
    assert conflict["files"] == 1


def test_empty_default_folder_is_not_a_conflict(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "marketer_data").mkdir()
    result = of.resolve_data_dir("marketer", str(workspace),
                                 prompt_text="MEMORY_DIR: bases/mkt", config={})
    assert result["conflicts"] == []


def test_resolution_is_pure_and_creates_nothing(tmp_path):
    before = sorted(os.listdir(tmp_path))
    of.resolve_data_dir("project_manager", str(tmp_path),
                        prompt_text="MEMORY_DIR: bases/pm", config={})
    assert sorted(os.listdir(tmp_path)) == before


def test_default_data_dir_name_uses_safe_slug():
    assert of.default_data_dir_name("Project Manager") == "project_manager_data"
    assert of.default_data_dir_name("") == "unnamed_data"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
