# -*- coding: utf-8 -*-
"""The data layer resolves an entity name for the interface language.

These tests drive the public read functions (the ones the UI and the prompt
builder call) and check which slot each language picks, plus the guarantee
that a read never turns a translation into the user-defined name.
"""
import os
import shutil
import tempfile

import pytest

from storage.db import reset_devagent_engine, reset_engine

TRANSLATIONS = {
    "name_en": "Developer",
    "name_ru": "Разработчик",
    "name_zh": "开发者",
}


@pytest.fixture()
def isolated_data_dir():
    """Temporary DATA_DIR plus a stored ``ui_lang`` the tests can switch."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_resolve_")
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


def _set_lang(name: str) -> None:
    """Store the interface language the way the settings page does."""
    from core.config import load_config, save_config

    cfg = dict(load_config())
    cfg["ui_lang"] = name
    assert save_config(cfg)


def _make_employee(slug: str = "named_agent") -> None:
    """Create an employee whose translations are set and user slot is empty."""
    from core.orchestrators import create_orchestrator, save_orchestrator

    assert create_orchestrator(slug, "", "", prompt_text="p")
    assert save_orchestrator(slug, **TRANSLATIONS)


def test_translations_follow_the_interface_language(isolated_data_dir):
    from core.orchestrators import get_orchestrator

    _make_employee()
    expected = {"English": "Developer", "Русский": "Разработчик",
                "简体中文": "开发者"}
    for lang, name in expected.items():
        _set_lang(lang)
        assert get_orchestrator("named_agent")["name"] == name


def test_an_unknown_language_falls_back_to_english(isolated_data_dir):
    from core.orchestrators import get_orchestrator

    _make_employee()
    _set_lang("Deutsch")
    assert get_orchestrator("named_agent")["name"] == "Developer"


def test_a_user_defined_name_wins_in_every_language(isolated_data_dir):
    """The owner typed one name; it shows unchanged in each locale."""
    from core.orchestrators import get_orchestrator

    _make_employee()
    from core.orchestrators import save_orchestrator
    assert save_orchestrator("named_agent", name="Мой помощник")
    for lang in ("English", "Русский", "简体中文"):
        _set_lang(lang)
        assert get_orchestrator("named_agent")["name"] == "Мой помощник"


def test_a_record_with_no_name_at_all_shows_its_slug(isolated_data_dir):
    from core.orchestrators import create_orchestrator, get_orchestrator

    assert create_orchestrator("bare_agent", "", "", prompt_text="p")
    _set_lang("Русский")
    assert get_orchestrator("bare_agent")["name"] == "bare_agent"


def test_reading_never_moves_a_translation_into_the_user_slot(isolated_data_dir):
    """A resolved read is a view; the stored slots stay as they were."""
    from core.orchestrators import get_orchestrator
    from storage.repository import repo_get_orchestrator_with_text

    _make_employee()
    _set_lang("Русский")
    view = get_orchestrator("named_agent")
    assert view["name"] == "Разработчик"
    assert view["name_user"] == ""

    stored = repo_get_orchestrator_with_text("named_agent")
    assert stored["name"] == ""
    assert stored["name_ru"] == "Разработчик"


def test_assistants_resolve_the_same_way(isolated_data_dir):
    from core.assistants import create_assistant, get_assistant_by_id, load_assistants_index
    from storage.repository import repo_update_assistant

    pid = create_assistant("", "YandexAI", "m", 0.3, "prompt")
    assert pid
    assert repo_update_assistant(
        assistant_id=pid, name="", service="YandexAI", model="m",
        temperature=0.3, prompt_text="prompt", **TRANSLATIONS
    )
    _set_lang("简体中文")
    assert get_assistant_by_id(pid)["name"] == "开发者"
    listed = [row for row in load_assistants_index() if row["id"] == pid]
    assert listed and listed[0]["name"] == "开发者"
    assert listed[0]["name_user"] == ""
