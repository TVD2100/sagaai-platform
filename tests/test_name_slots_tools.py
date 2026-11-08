# -*- coding: utf-8 -*-
"""The creation and editing tools carry all four name slots.

An entity keeps a user-defined name next to its en/ru/zh translations. The
editing tools must move exactly the slots the caller passed: an omitted slot
keeps its stored value, an explicitly passed empty string clears it, and a call
that would leave the record with no label at all is refused.
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
    """Temporary DATA_DIR so the tests never touch the real installation."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_name_slots_")
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


def _assistant_tools():
    """Return the assistant tool mixin bound to the real data layer."""
    from dev_agent.tools.assistants import AssistantTools

    class _Tools(AssistantTools):
        pass

    return _Tools()


def _make_assistant(**slots) -> str:
    """Create an assistant directly through the data layer."""
    from core.assistants import create_assistant

    name = slots.pop("name", "")
    pid = create_assistant(name, "YandexAI", "m", 0.3, "prompt", **slots)
    assert pid
    return pid


def _stored(pid: str) -> dict:
    """Read the raw stored row, bypassing the language resolution."""
    from storage.repository import repo_get_assistant_with_text

    row = repo_get_assistant_with_text(pid)
    assert row is not None
    return row


def test_update_adds_a_translation_without_touching_the_user_slot(isolated_data_dir):
    pid = _make_assistant(name="Мой помощник")
    result = _assistant_tools().update_assistant_by_id(pid, name_zh="我的助手")
    assert result["ok"] is True, result

    stored = _stored(pid)
    assert stored["name"] == "Мой помощник"
    assert stored["name_zh"] == "我的助手"
    assert stored["name_en"] == ""


def test_update_clears_one_translation_and_keeps_the_others(isolated_data_dir):
    pid = _make_assistant(**TRANSLATIONS)
    result = _assistant_tools().update_assistant_by_id(pid, name_ru="")
    assert result["ok"] is True, result

    stored = _stored(pid)
    assert stored["name_ru"] == ""
    assert stored["name_en"] == "Developer"
    assert stored["name_zh"] == "开发者"


def test_update_keeps_the_stored_user_slot_when_the_view_resolved_it(isolated_data_dir):
    """Editing a translation must not copy the resolved name into the user slot."""
    pid = _make_assistant(**TRANSLATIONS)
    result = _assistant_tools().update_assistant_by_id(pid, description="d")
    assert result["ok"] is True, result

    stored = _stored(pid)
    assert stored["name"] == ""
    assert stored["name_en"] == "Developer"


def test_update_refuses_to_strip_every_name(isolated_data_dir):
    pid = _make_assistant(name=None, **TRANSLATIONS)
    result = _assistant_tools().update_assistant_by_id(
        pid, name="", name_en="", name_ru="", name_zh="")
    assert result["ok"] is False
    assert "without a name" in result["error"]

    stored = _stored(pid)
    assert stored["name_en"] == "Developer"


def test_update_reports_the_resolved_name(isolated_data_dir):
    pid = _make_assistant(name="Мой помощник")
    result = _assistant_tools().update_assistant_by_id(pid, name="Новое имя")
    assert result["ok"] is True, result
    assert result["assistant_name"] == "Новое имя"


def test_merged_slots_fall_back_to_the_plain_name_of_a_legacy_record():
    """A record without ``name_user`` keeps its name in the user slot."""
    from core.naming import merge_slots

    slots = merge_slots({"name": "Старое имя"})
    assert slots["name"] == "Старое имя"


def test_update_orchestrator_accepts_a_translation_only_change(isolated_data_dir):
    from dev_agent.universal_agent import UniversalDevAgent

    agent = UniversalDevAgent()
    created = agent.dispatch("create_orchestrator", {
        "slug": "name_slots_agent", "name": "Мой сотрудник"})
    assert created["ok"] is True, created

    result = agent.dispatch("update_orchestrator", {
        "slug": "name_slots_agent", "name_zh": "我的员工"})
    assert result["ok"] is True, result

    from storage.repository import repo_get_orchestrator_with_text

    stored = repo_get_orchestrator_with_text("name_slots_agent")
    assert stored["name"] == "Мой сотрудник"
    assert stored["name_zh"] == "我的员工"


def test_update_orchestrator_refuses_to_strip_every_name(isolated_data_dir):
    from dev_agent.universal_agent import UniversalDevAgent

    agent = UniversalDevAgent()
    created = agent.dispatch("create_orchestrator", {
        "slug": "bare_slots_agent", "name": "", "name_ru": "Сотрудник"})
    assert created["ok"] is True, created

    result = agent.dispatch("update_orchestrator", {
        "slug": "bare_slots_agent", "name_ru": ""})
    assert result["ok"] is False
    assert "without a name" in result["error"]


def test_create_requires_at_least_one_name(isolated_data_dir):
    from dev_agent.universal_agent import UniversalDevAgent

    agent = UniversalDevAgent()
    result = agent.dispatch("create_orchestrator", {"slug": "nameless_agent"})
    assert result["ok"] is False
    assert "at least one name" in result["error"]
