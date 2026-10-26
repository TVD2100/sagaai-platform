# -*- coding: utf-8 -*-
"""
Tests for personal-memory prompt injection.

Two blocks are appended to a system prompt: the owner profile card (shared by
all employees, capped, present only when the card has content) and the memory
rules block. The rules block is emitted for EVERY employee - a fresh install
has no notes yet, and the practice never starts unless the prompt asks for it -
so it must teach both where the notes live and when to write them. Note texts
are never inlined.
"""
import pytest

import core.paths
from core import orchestrators as orch
from core import personal_memory as pm


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp directory so memory never touches real data."""
    monkeypatch.setattr(core.paths, "DATA_DIR", str(tmp_path))
    yield tmp_path


# ─── Owner card ──────────────────────────────────────────────────────────────


def test_card_block_is_appended(isolated_data_dir):
    pm.write_memory("user", "## Rules\n- обращаться на «вы»\n\n## Preferences\n- кратко")
    result = orch._extend_prompt_with_personal_card("BASE PROMPT", "marketer")
    assert result.startswith("BASE PROMPT\n\n## Owner profile")
    assert "обращаться на «вы»" in result
    assert "кратко" in result


def test_card_is_absent_when_memory_is_empty(isolated_data_dir):
    assert orch._extend_prompt_with_personal_card("BASE", "marketer") == "BASE"


def test_card_is_capped(isolated_data_dir):
    body = "## Notes\n" + "\n".join(f"- заметка номер {i}" for i in range(400))
    pm.write_memory("user", body)
    result = orch._extend_prompt_with_personal_card("BASE", "marketer")
    card = result.split("## Owner profile", 1)[1]
    assert len(card) <= pm.PERSONAL_CARD_HARD_CAP + 400


def test_card_works_as_whole_prompt(isolated_data_dir):
    pm.write_memory("user", "## Rules\n- правило")
    result = orch._extend_prompt_with_personal_card("", "marketer")
    assert result.startswith("## Owner profile")


def test_card_survives_broken_memory_layer(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("memory layer is down")
    monkeypatch.setattr(pm, "render_owner_card", boom)
    assert orch._extend_prompt_with_personal_card("BASE", "marketer") == "BASE"


# ─── Employee memory block ───────────────────────────────────────────────────


def test_memory_block_reports_size_without_notes(isolated_data_dir):
    secret_note = "- секретная заметка сотрудника"
    pm.write_memory("orchestrator:marketer", "## Notes\n" + secret_note)
    result = orch._extend_prompt_with_memory("BASE", "marketer")
    assert "## Memory" in result
    assert "orchestrator:marketer" in result
    assert "get_personal_memory" in result
    # The notes stay out of the prompt - only their size is reported.
    assert secret_note not in result


def test_memory_block_is_present_without_any_note(isolated_data_dir):
    """A fresh install must still learn that keeping notes is part of the job."""
    result = orch._extend_prompt_with_memory("BASE", "marketer")
    assert result.startswith("BASE\n\n## Memory")
    assert "is empty" in result
    assert "orchestrator:marketer" in result


def test_memory_block_teaches_when_to_write(isolated_data_dir):
    result = orch._extend_prompt_with_memory("BASE", "marketer")
    assert "set_personal_memory" in result
    # 'append' is the way to extend a scope; 'replace' is not advertised.
    assert "append" in result
    # The triggers must be named, otherwise the rule is only about mechanics.
    assert "corrects your approach" in result
    assert "settle a decision" in result


def test_memory_block_protects_the_owner_profile(isolated_data_dir):
    result = orch._extend_prompt_with_memory("BASE", "marketer")
    assert "owner profile" in result
    assert "overwrite_owner_profile" in result


def test_memory_block_is_per_employee(isolated_data_dir):
    pm.write_memory("orchestrator:marketer", "## Notes\n- маркетинг")
    other = orch._extend_prompt_with_memory("BASE", "project_manager")
    own = orch._extend_prompt_with_memory("BASE", "marketer")
    # Every employee gets the rules block, but each names its OWN scope and
    # never leaks the notes of another employee.
    assert "orchestrator:project_manager" in other
    assert "orchestrator:marketer" in own
    assert "маркетинг" not in other


def test_memory_block_survives_broken_memory_layer(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("memory layer is down")
    monkeypatch.setattr(pm, "memory_meta", boom)
    result = orch._extend_prompt_with_memory("BASE", "marketer")
    # A broken size reporter must not silence the rules themselves.
    assert "## Memory" in result
    assert "is empty" in result


# ─── Both blocks in the assembled prompt ─────────────────────────────────────


def test_both_blocks_are_independent(isolated_data_dir):
    pm.write_memory("user", "## Rules\n- правило владельца")
    pm.write_memory("orchestrator:marketer", "## Notes\n- заметка сотрудника")
    first = orch._extend_prompt_with_personal_card("BASE", "marketer")
    both = orch._extend_prompt_with_memory(first, "marketer")
    assert "## Owner profile" in both
    assert "## Memory" in both
    assert both.index("## Owner profile") < both.index("## Memory")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
