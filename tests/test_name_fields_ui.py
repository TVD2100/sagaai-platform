# -*- coding: utf-8 -*-
"""tests/test_name_fields_ui.py - the Names block of an entity form.

Renders ui.components.name_fields and the assistant form under the
Streamlit mock, and verifies:
- four inputs are rendered with real tooltips and stable keys;
- an existing record pre-fills the user slot and its translations;
- the entered translations reach the assistant save call;
- a name typed only in the user slot is accepted without translations;
- the orchestrator names section saves all four slots through
  save_orchestrator.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_ui_tooltips import _invoke, mock_env  # noqa: F401
from ui.components.name_fields import name_widget_key, render_name_fields


def test_four_inputs_with_tooltips_and_keys(mock_env):
    """The block renders one user input and three translation inputs."""
    values = render_name_fields("English", "abc123", {}, st=mock_env)

    calls = {kwargs.get("key"): kwargs for name, _a, kwargs in mock_env.calls
             if name == "text_input"}
    for slot in ("user", "en", "ru", "zh"):
        key = name_widget_key("abc123", slot)
        assert key in calls, f"input {slot} was not rendered"
        help_text = calls[key].get("help")
        assert isinstance(help_text, str) and help_text.strip()
        assert help_text != "names_user_help"
        assert help_text != "names_translation_help"

    assert values == {"name": "", "name_en": "", "name_ru": "", "name_zh": ""}


def test_existing_slots_prefill_and_are_returned(mock_env):
    """A record's own values become the widget defaults."""
    record = {"name": "", "name_en": "Developer", "name_ru": "Разработчик",
              "name_zh": ""}
    mock_env._text_returns[name_widget_key("x", "zh")] = "开发者"
    values = render_name_fields("Русский", "x", record, st=mock_env)

    calls = {kwargs.get("key"): kwargs for name, _a, kwargs in mock_env.calls
             if name == "text_input"}
    assert calls[name_widget_key("x", "en")]["value"] == "Developer"
    assert calls[name_widget_key("x", "ru")]["value"] == "Разработчик"
    assert calls[name_widget_key("x", "zh")]["value"] == ""
    assert values["name_zh"] == "开发者"


def test_a_historical_user_key_is_honoured(mock_env):
    """A caller may keep the widget key its form had before translations."""
    render_name_fields("English", "new", {}, st=mock_env,
                       user_key="assistant_name_input")
    keys = [kwargs.get("key") for name, _a, kwargs in mock_env.calls
            if name == "text_input"]
    assert "assistant_name_input" in keys


def _render_assistant_form(mock_env, existing, save_mock):
    """Render the assistant form in edit mode with the given stored record."""
    import ui.pages.assistants as mod

    mock_env.click("assistant_save_btn")
    with patch.object(mod, "get_services", return_value={"TestSvc": {}}), \
         patch.object(mod, "service_supported_tools", return_value=[]), \
         patch.object(mod, "list_tool_definitions", return_value=[]), \
         patch.object(mod, "list_rag_bases", return_value=[]), \
         patch.object(mod, "_get_edit_id", return_value=existing["id"]), \
         patch.object(mod, "_prepare_prompt_revision", return_value=(0, "p")), \
         patch.object(mod, "_form_defaults", return_value=({
             "name": existing.get("name_user", ""),
             "name_en": existing.get("name_en", ""),
             "name_ru": existing.get("name_ru", ""),
             "name_zh": existing.get("name_zh", ""),
             "service": "TestSvc", "model": "m", "temp": 0.2, "desc": "",
             "text": "prompt", "tools": [], "max_calls": 3, "max_tokens": None,
             "reasoning": None, "ws_settings": {},
         }, existing)), \
         patch.object(mod, "update_assistant", save_mock), \
         patch.object(mod, "set_assistant_rag_bases"), \
         patch.object(mod, "set_assistant_web_search_settings"):
        _invoke(lambda: mod._render_assistant_form("English", {"TestSvc": {}},
                                                   ["TestSvc"], []))


def test_assistant_form_saves_all_four_slots(mock_env):
    """Translations typed into the form reach update_assistant."""
    existing = {"id": "a1", "slug": "s", "name_user": "", "name_en": "",
                "name_ru": "", "name_zh": ""}
    mock_env._text_returns[name_widget_key("a1", "en")] = "Developer"
    mock_env._text_returns[name_widget_key("a1", "ru")] = "Разработчик"
    save_mock = MagicMock(return_value=True)
    _render_assistant_form(mock_env, existing, save_mock)

    save_mock.assert_called_once()
    kwargs = save_mock.call_args.kwargs
    assert kwargs["name_en"] == "Developer"
    assert kwargs["name_ru"] == "Разработчик"
    assert kwargs["name_zh"] is None


def test_assistant_form_accepts_a_user_name_without_translations(mock_env):
    """A locally named assistant needs no translation to save."""
    existing = {"id": "a2", "slug": "s2", "name_user": "", "name_en": "",
                "name_ru": "", "name_zh": ""}
    mock_env._text_returns["assistant_name_input"] = "Мой помощник"
    save_mock = MagicMock(return_value=True)
    _render_assistant_form(mock_env, existing, save_mock)

    save_mock.assert_called_once()
    assert save_mock.call_args.args[1] == "Мой помощник"
    assert not mock_env.errors


def test_orchestrator_names_section_saves_all_slots(mock_env):
    """The Names section of the Other tab persists the four slots."""
    import ui.pages.orchestrator as orch_mod

    orch = {"name": "", "name_en": "", "name_ru": "", "name_zh": ""}
    mock_env.click("orch_save_names_agent")
    mock_env._text_returns[name_widget_key("agent", "zh")] = "开发者"
    save_mock = MagicMock(return_value=True)
    with patch.object(orch_mod, "get_orchestrator", return_value=orch), \
         patch.object(orch_mod, "save_orchestrator", save_mock):
        _invoke(lambda: orch_mod._render_other_settings("agent", "English"))

    save_mock.assert_called_once_with(
        "agent", name="", name_en="", name_ru="", name_zh="开发者")
