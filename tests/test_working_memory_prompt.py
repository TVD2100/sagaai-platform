# -*- coding: utf-8 -*-
"""
Tests for the platform working-memory block of a system prompt.

The block announces the employee's working-data folder and the ``memory:``
addressing scheme. It is emitted for EVERY employee, so a role prompt never
repeats the path and cannot drift away from the resolver.
"""
import os

import pytest

import core.paths
from core import orchestrator_folders as of
from core import orchestrators as orch


@pytest.fixture()
def central(tmp_path, monkeypatch):
    folder = tmp_path / "agents_data"
    folder.mkdir()
    monkeypatch.setattr(core.paths, "AGENTS_DATA_DIR", str(folder))
    yield folder


@pytest.fixture()
def no_records(monkeypatch):
    """Serve the DEFAULT branch: no stored record for any slug."""
    monkeypatch.setattr(orch, "get_orchestrator", lambda slug: None)


@pytest.fixture()
def project(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


# ─── The block itself ────────────────────────────────────────────────────────


def test_block_is_appended_for_an_employee(central, project, no_records):
    out = orch._extend_prompt_with_working_memory("# ROLE\nBody", "marketer", str(project))
    assert out.startswith("# ROLE\nBody"), "the role prompt must stay first"
    assert "## Working memory" in out


def test_block_reports_the_central_folder(central, project, no_records):
    out = orch._extend_prompt_with_working_memory("x", "marketer", str(project))
    assert str(central / "marketer") in out
    assert "set by: `default`" in out
    assert "scope: `central`" in out


def test_block_explains_addressing(central, project, no_records):
    out = orch._extend_prompt_with_working_memory("x", "marketer", str(project))
    assert "memory:" in out
    assert "memory:registry.md" in out
    assert "read_file" in out and "propose_file" in out


def test_block_states_project_independence(central, project, no_records):
    out = orch._extend_prompt_with_working_memory("x", "marketer", str(project))
    assert "does not depend on the project" in out


def test_block_works_without_a_selected_project(central, no_records):
    out = orch._extend_prompt_with_working_memory("x", "marketer", "")
    assert str(central / "marketer") in out


def test_block_is_created_for_a_prompt_without_text(central, project, no_records):
    out = orch._extend_prompt_with_working_memory("", "marketer", str(project))
    assert out.startswith("## Working memory")


# ─── Folder state and overrides ─────────────────────────────────────────────


def test_block_mentions_a_missing_folder(central, project, no_records):
    out = orch._extend_prompt_with_working_memory("x", "brand_new", str(project))
    assert "does not exist yet" in out


def test_block_omits_the_missing_note_when_present(central, project, no_records):
    (central / "marketer").mkdir()
    out = orch._extend_prompt_with_working_memory("x", "marketer", str(project))
    assert "does not exist yet" not in out


def test_block_carries_the_external_override(central, tmp_path):
    """A MEMORY_DIR marker is honoured here exactly as in the resolver."""
    external = tmp_path / "external_desk"
    external.mkdir()
    prompt = f"# ROLE\nMEMORY_DIR: {external}\n"
    original = orch.get_orchestrator
    orch.get_orchestrator = lambda slug: {"prompt_text": prompt, "config": {}}
    try:
        out = orch._extend_prompt_with_working_memory("x", "editor", "")
    finally:
        orch.get_orchestrator = original
    assert str(external) in out
    assert "scope: `external`" in out


def test_block_warns_about_a_legacy_folder(central, project, no_records):
    legacy = project / "marketer_data"
    legacy.mkdir()
    (legacy / "registry.md").write_text("# r", encoding="utf-8")
    out = orch._extend_prompt_with_working_memory("x", "marketer", str(project))
    assert "previous layout" in out
    assert str(legacy) in out


def test_block_never_breaks_the_prompt_on_resolver_failure(central, project, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(of, "resolve_data_dir", boom)
    out = orch._extend_prompt_with_working_memory("# ROLE", "marketer", str(project))
    assert out == "# ROLE"


def test_block_skips_an_unresolvable_slug(central, project, monkeypatch):
    monkeypatch.setattr(of, "resolve_data_dir",
                        lambda *a, **k: {"path": "", "source": "default"})
    out = orch._extend_prompt_with_working_memory("# ROLE", "marketer", str(project))
    assert out == "# ROLE"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
