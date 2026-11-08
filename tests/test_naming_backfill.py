# -*- coding: utf-8 -*-
"""tests/test_naming_backfill.py - shipped names become translations.

The backfill is the only place that may rewrite an existing name, so every
rule it follows is pinned here:

- a record holding a name the platform shipped moves that name into the
  translation slots and frees the user-defined slot;
- a record the owner renamed is never touched;
- a record that already carries a translation is left alone;
- an empty record simply receives the preset slots.

On top of the pure rules, the installed defaults are checked end to end:
a record seeded by an older version is migrated on the next boot, and a
renamed one survives it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.naming_backfill import (  # noqa: E402
    SHIPPED_NAMES,
    default_name_backfill,
    shipped_names_for,
)
from storage.db import reset_devagent_engine, reset_engine  # noqa: E402

PRESET = {
    "name_user": "",
    "name_en": "Task Scout",
    "name_ru": "Разведчик задач",
    "name_zh": "任务侦察员",
}

#: The fields the backfill writes: the user-defined slot is cleared and the
#: three translations carry the shipped names.
EXPECTED = {
    "name": "",
    "name_en": "Task Scout",
    "name_ru": "Разведчик задач",
    "name_zh": "任务侦察员",
}


@pytest.fixture
def platform(tmp_path, monkeypatch):
    """A fresh data dir so seeded rows and memory folders are isolated."""
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
    yield tmp_path
    reset_engine()
    reset_devagent_engine()


# ─── the pure rules ──────────────────────────────────────────────────────


def test_a_shipped_name_moves_into_the_translations():
    current = {"name_user": "Task Scout", "name_en": "", "name_ru": "", "name_zh": ""}

    fields = default_name_backfill(current, PRESET, SHIPPED_NAMES["task_scout"])

    assert fields == EXPECTED
    # The user-defined slot is freed, so the interface follows its language.
    assert fields["name"] == ""


def test_a_renamed_entity_keeps_its_name():
    current = {"name_user": "Мой разведчик", "name_en": "", "name_ru": "", "name_zh": ""}

    fields = default_name_backfill(current, PRESET, SHIPPED_NAMES["task_scout"])

    assert fields == {}


def test_an_entity_with_a_translation_is_left_alone():
    current = {"name_user": "", "name_en": "Task Scout", "name_ru": "", "name_zh": ""}

    fields = default_name_backfill(current, PRESET, SHIPPED_NAMES["task_scout"])

    assert fields == {}


def test_an_empty_record_receives_the_preset_slots():
    current = {"name_user": "", "name_en": "", "name_ru": "", "name_zh": ""}

    fields = default_name_backfill(current, PRESET, ())

    assert fields == EXPECTED


def test_a_preset_without_any_name_changes_nothing():
    current = {"name_user": "Task Scout"}

    assert default_name_backfill(current, {}, ()) == {}


def test_an_old_translation_counts_as_shipped_too():
    """A record may already hold the new English name in the user slot."""
    current = {"name_user": "Разведчик задач"}

    shipped = shipped_names_for("task_scout", PRESET)

    assert "Разведчик задач" in shipped
    assert default_name_backfill(current, PRESET, shipped) == EXPECTED


def test_shipped_names_cover_every_bundled_entity():
    """The table is the authority for what an older version shipped."""
    for slug in ("dev_agent", "task_scout", "universal_agent", "ya_agent",
                 "korrektor", "universalnyy_tyutor"):
        assert SHIPPED_NAMES[slug], slug


# ─── the installed defaults ──────────────────────────────────────────────


def test_a_devagent_row_seeded_with_the_old_name_is_migrated(platform):
    """The built-in employee takes the agreed names on the next boot."""
    from core.orchestrator_builtin import ensure_builtin_orchestrators
    from core.orchestrators import DEVAGENT_SLUG, get_orchestrator

    ensure_builtin_orchestrators()

    # Simulate a record created by an older version: one shipped name in the
    # user-defined slot and no translations.
    from storage.repository import repo_get_orchestrator_with_text, repo_update_orchestrator

    row = repo_get_orchestrator_with_text(DEVAGENT_SLUG)
    repo_update_orchestrator(row["id"], name="DevAgent", name_en=None,
                             name_ru=None, name_zh=None)

    ensure_builtin_orchestrators()

    migrated = get_orchestrator(DEVAGENT_SLUG)
    assert migrated["name_user"] == ""
    assert migrated["name_en"] == "Developer"
    assert migrated["name_ru"] == "Разработчик"
    assert migrated["name_zh"] == "开发者"


def test_a_renamed_devagent_survives_the_boot(platform):
    from core.orchestrator_builtin import ensure_builtin_orchestrators
    from core.orchestrators import DEVAGENT_SLUG, get_orchestrator
    from storage.repository import repo_get_orchestrator_with_text, repo_update_orchestrator

    ensure_builtin_orchestrators()
    row = repo_get_orchestrator_with_text(DEVAGENT_SLUG)
    repo_update_orchestrator(row["id"], name="Мой разработчик")

    ensure_builtin_orchestrators()

    renamed = get_orchestrator(DEVAGENT_SLUG)
    assert renamed["name_user"] == "Мой разработчик"
    # A user-defined name wins in every language.
    assert renamed["name"] == "Мой разработчик"


def test_a_seeded_default_orchestrator_carries_three_translations(platform):
    from core.default_imports import ensure_default_orchestrators
    from core.orchestrators import get_orchestrator

    ensure_default_orchestrators()

    scout = get_orchestrator("task_scout")
    assert scout["name_user"] == ""
    assert (scout["name_en"], scout["name_ru"], scout["name_zh"]) == (
        "Task Scout", "Разведчик задач", "任务侦察员")


def test_an_old_task_scout_row_is_migrated_on_the_next_boot(platform):
    """A default installed before the translations moves to the new scheme."""
    from core.default_imports import ensure_default_orchestrators
    from core.orchestrators import get_orchestrator
    from storage.repository import repo_get_orchestrator_with_text, repo_update_orchestrator

    ensure_default_orchestrators()
    row = repo_get_orchestrator_with_text("task_scout")
    repo_update_orchestrator(row["id"], name="Task Scout", name_en=None,
                             name_ru=None, name_zh=None)

    ensure_default_orchestrators()

    migrated = get_orchestrator("task_scout")
    assert migrated["name_user"] == ""
    assert migrated["name_ru"] == "Разведчик задач"


def test_the_display_name_does_not_depend_on_the_language(platform):
    """A loader returns the same bundle whoever reads it."""
    from core.defaults import load_default_orchestrator

    first = load_default_orchestrator("universal_agent")
    second = load_default_orchestrator("universal_agent")

    assert first["display_name"] == second["display_name"] == "Universal Agent"


# ─── the bundled assistants ──────────────────────────────────────────────


def test_a_seeded_assistant_carries_three_translations(platform):
    from core.assistants import load_assistants_index
    from core.default_imports import ensure_default_assistants

    ensure_default_assistants()

    rows = {row["name_en"]: row for row in load_assistants_index()}
    proofreader = rows["Proofreader"]
    assert proofreader["name_user"] == ""
    assert (proofreader["name_ru"], proofreader["name_zh"]) == ("Корректор", "校对员")


def test_an_old_assistant_row_is_migrated_on_the_next_boot(platform):
    """A copy installed before the translations moves to the new scheme."""
    from core.assistants import load_assistants_index
    from core.default_imports import ensure_default_assistants
    from storage.repository import repo_update_assistant_names

    ensure_default_assistants()
    row = next(r for r in load_assistants_index() if r["name_en"] == "Proofreader")
    repo_update_assistant_names(row["id"], "Proofreader", name_en="", name_ru="", name_zh="")

    status = ensure_default_assistants()

    assert status["Proofreader"] == "exists"
    migrated = next(r for r in load_assistants_index() if r["id"] == row["id"])
    assert migrated["name_user"] == ""
    assert migrated["name_en"] == "Proofreader"
    assert migrated["name_ru"] == "Корректор"


def test_a_renamed_assistant_is_neither_recreated_nor_renamed_back(platform):
    """The owner's name survives a boot, and no second copy appears."""
    from core.assistants import load_assistants_index
    from core.default_imports import ensure_default_assistants
    from storage.repository import repo_update_assistant_names

    ensure_default_assistants()
    before = len(load_assistants_index())
    row = next(r for r in load_assistants_index() if r["name_en"] == "Proofreader")
    repo_update_assistant_names(row["id"], "Мой корректор", name_en="", name_ru="", name_zh="")

    status = ensure_default_assistants()

    assert status["Proofreader"] == "exists"
    rows = load_assistants_index()
    assert len(rows) == before
    renamed = next(r for r in rows if r["id"] == row["id"])
    assert renamed["name"] == "Мой корректор"


def test_the_backfill_writes_only_the_name_slots(platform):
    """A migration must not touch the prompt, the model or the tool limits."""
    from core.assistants import load_assistants_index
    from core.default_imports import ensure_default_assistants
    from storage.repository import repo_get_assistant_with_text, repo_update_assistant_names

    ensure_default_assistants()
    row = next(r for r in load_assistants_index() if r["name_en"] == "Proofreader")
    repo_update_assistant_names(row["id"], "Proofreader", name_en="", name_ru="", name_zh="")
    before = repo_get_assistant_with_text(row["id"])

    ensure_default_assistants()

    after = repo_get_assistant_with_text(row["id"])
    assert after["text"] == before["text"]
    assert after["model"] == before["model"]
    assert after["service"] == before["service"]
    assert after["temperature"] == before["temperature"]
    assert after["max_tokens"] == before["max_tokens"]
    assert after["name_ru"] == "Корректор"

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
