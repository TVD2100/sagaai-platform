# -*- coding: utf-8 -*-
"""Round-trip tests: the four name slots survive DB -> folder -> DB.

An orchestrator takes its truth from ``orchestrator.json`` and an assistant
from ``manifest.json``; both files must carry the same four slots the DB
columns do, otherwise a reload at the next start would clear a translation
nobody retyped.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from storage.db import reset_devagent_engine, reset_engine


@pytest.fixture()
def isolated_data_dir():
    """Temporary DATA_DIR so no real DB or folder is touched."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_names_")
    old_data_dir = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths
    old_values = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR",
                 "SYSTEM_PROMPTS_DIR"):
        old_values[attr] = getattr(core.paths, attr, None)

    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    reset_engine()
    reset_devagent_engine()
    try:
        yield tmp
    finally:
        reset_engine()
        reset_devagent_engine()
        if old_data_dir:
            os.environ["SAGAAI_DATA_DIR"] = old_data_dir
        else:
            os.environ.pop("SAGAAI_DATA_DIR", None)
        for attr, value in old_values.items():
            if value is not None:
                setattr(core.paths, attr, value)
        shutil.rmtree(tmp, ignore_errors=True)


TRANSLATIONS = {
    "name_en": "Developer",
    "name_ru": "Разработчик",
    "name_zh": "开发者",
}


def test_orchestrator_slots_survive_a_folder_round_trip(isolated_data_dir):
    from core.orchestrator_store import (
        reload_orchestrator_from_folder,
        save_orchestrator,
    )
    from core.orchestrators import create_orchestrator, get_orchestrator

    assert create_orchestrator(
        slug="dev_local", name="", description="", prompt_text="p"
    )
    assert save_orchestrator("dev_local", **TRANSLATIONS)

    bundle_path = Path(isolated_data_dir) / "orchestrators" / "dev_local" / "orchestrator.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    for field, value in TRANSLATIONS.items():
        assert bundle[field] == value

    # Simulate a start: the folder is reloaded into the DB.
    assert reload_orchestrator_from_folder("dev_local")["ok"]
    record = get_orchestrator("dev_local")
    for field, value in TRANSLATIONS.items():
        assert record[field] == value
    # The read view resolves ``name`` for the interface language, so the empty
    # user-defined slot is visible as ``name_user``.
    assert record["name_user"] == ""
    assert record["name"] in TRANSLATIONS.values()


def test_orchestrator_import_accepts_a_name_mapping(isolated_data_dir):
    """A marketplace bundle may carry one name mapping instead of four keys."""
    from core.orchestrator_transfer import _EXPORT_FORMAT, import_orchestrator
    from core.orchestrators import get_orchestrator

    data = {
        "format": _EXPORT_FORMAT,
        "slug": "mapped_agent",
        "name": {"en": "Mapped", "ru": "Сопоставленный", "zh-CN": "映射"},
        "description": "",
        "prompt_text": "prompt",
        "config": {},
        "tools": [],
    }
    result = import_orchestrator(data)
    assert result["ok"], result

    record = get_orchestrator("mapped_agent")
    assert record["name_user"] == ""
    assert record["name_en"] == "Mapped"
    assert record["name_ru"] == "Сопоставленный"
    assert record["name_zh"] == "映射"
    assert record["name"] in ("Mapped", "Сопоставленный", "映射")


def test_assistant_slots_survive_a_folder_round_trip(isolated_data_dir):
    from core.assistant_folders import sync_assistant_to_folder
    from core.assistants import create_assistant, reload_assistant_from_folder
    from storage.repository import repo_get_assistant_with_text, repo_update_assistant

    pid = create_assistant(name="", service="YandexAI", model="m",
                           temperature=0.3, text="prompt")
    assert pid
    assert repo_update_assistant(
        assistant_id=pid, name="", service="YandexAI", model="m",
        temperature=0.3, prompt_text="prompt", **TRANSLATIONS
    )
    full = repo_get_assistant_with_text(pid)
    assert sync_assistant_to_folder(full)

    slug = full["slug"]
    manifest_path = Path(isolated_data_dir) / "assistants" / slug / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for field, value in TRANSLATIONS.items():
        assert manifest[field] == value

    # The manifest is the durable side: wiping the DB slots and reloading the
    # folder must bring them back.
    assert repo_update_assistant(
        assistant_id=pid, name="", service="YandexAI", model="m",
        temperature=0.3, prompt_text="prompt",
        name_en="", name_ru="", name_zh=""
    )
    assert reload_assistant_from_folder(slug)["ok"]
    record = repo_get_assistant_with_text(pid)
    for field, value in TRANSLATIONS.items():
        assert record[field] == value


def test_assistant_import_accepts_a_name_mapping(isolated_data_dir):
    from core.assistants import import_assistant
    from storage.repository import repo_get_assistant_by_slug

    data = {
        "format": "sagaai_assistant/v1",
        "slug": "proofreader",
        "name": {"en": "Proofreader", "ru": "Корректор", "zh-CN": "校对员"},
        "service": "YandexAI",
        "model": "m",
        "temperature": 0.3,
        "prompt_text": "prompt",
        "tools": [],
    }
    result = import_assistant(data)
    assert result["ok"], result

    record = repo_get_assistant_by_slug("proofreader")
    assert record["name"] == ""
    assert record["name_en"] == "Proofreader"
    assert record["name_ru"] == "Корректор"
    assert record["name_zh"] == "校对员"
