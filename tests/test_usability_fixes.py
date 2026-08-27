# -*- coding: utf-8 -*-
"""tests/test_usability_fixes.py - UI regression tests for the usability fixes.

Covers the fixes of the SagaAI usability-test round:
  - two-step delete confirmations (assistants, skills library, employees),
  - neutral caption on the employee chat page,
  - (further fixes are added in the follow-up steps).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture()
def ui_env(monkeypatch, tmp_path):
    """Isolated DATA_DIR + fresh ui.* modules under the streamlit mock."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    for m in list(sys.modules):
        if m == "ui" or m.startswith("ui."):
            sys.modules.pop(m, None)
    with install_streamlit_mock() as st:
        yield st


def _rerender(st, fn):
    try:
        fn()
    except StopRerun:
        pass


def _button_keys(st):
    return [c[2].get("key") for c in st.calls if c[0] == "button"]


def _captions(st):
    return [c[1][0] for c in st.calls if c[0] == "caption"]


# ─── two-step delete confirmations ───────────────────────────────────────────

def test_assistant_delete_requires_confirmation(ui_env, monkeypatch):
    assistant = {
        "id": "a1", "name": "Test Assistant", "slug": "test_assistant",
        "service": "DeepSeek", "model": "m1", "temperature": 0.7,
        "description": "", "text": "prompt", "created_at": "2026-01-01T00:00:00",
        "tools": [], "max_tokens": None, "reasoning_effort": None,
        "max_tool_calls": None,
    }
    deleted = []
    import core.assistants as assistants_mod
    import core.services as services_mod
    monkeypatch.setattr(assistants_mod, "load_assistants_index", lambda: [assistant])
    monkeypatch.setattr(assistants_mod, "get_assistant_by_id",
                        lambda aid: assistant if aid == "a1" else None)
    monkeypatch.setattr(assistants_mod, "list_assistant_files", lambda aid: [])
    monkeypatch.setattr(assistants_mod, "delete_assistant",
                        lambda aid: deleted.append(aid))
    monkeypatch.setattr(services_mod, "get_services", lambda: {})

    from ui.pages.assistants import page_assistants

    st = ui_env
    st.session_state.update({"ui_lang": "English"})
    _rerender(st, page_assistants)

    st.click("del_a1")
    _rerender(st, page_assistants)
    st.reset_clicks()
    _rerender(st, page_assistants)

    assert deleted == [], "assistant delete fired without confirmation"
    assert any("Deleting" in str(w) for w in st.warnings), \
        f"no confirmation warning: {st.warnings}"
    keys = _button_keys(st)
    assert "del_yes_a1" in keys, f"yes button missing: {keys}"
    assert "del_no_a1" in keys, f"no button missing: {keys}"

    st.click("del_yes_a1")
    _rerender(st, page_assistants)
    assert deleted == ["a1"]


def test_skills_library_delete_requires_confirmation(ui_env, monkeypatch):
    skill = {"id": "abc123", "name": "Rag Skill",
             "description": "", "folder": "Rag_Skill"}
    deleted = []
    import core.skills_library as slib_mod
    monkeypatch.setattr(slib_mod, "list_skills", lambda: [skill])
    monkeypatch.setattr(slib_mod, "get_skill",
                        lambda sid: skill if sid == "abc123" else None)
    monkeypatch.setattr(slib_mod, "get_skills_root", lambda: "/tmp/skills")
    monkeypatch.setattr(slib_mod, "delete_skill",
                        lambda sid: deleted.append(sid))

    from ui.pages.skills_library import page_skills_library

    st = ui_env
    st.session_state.update({"ui_lang": "English", "slib_edit_id": "abc123"})
    _rerender(st, page_skills_library)

    st.click("slib_del_btn")
    _rerender(st, page_skills_library)
    st.reset_clicks()
    _rerender(st, page_skills_library)

    assert deleted == [], "skill delete fired without confirmation"
    assert any("Deleting" in str(w) for w in st.warnings), \
        f"no confirmation warning: {st.warnings}"
    keys = _button_keys(st)
    assert "slib_del_yes_btn" in keys, f"yes button missing: {keys}"
    assert "slib_del_no_btn" in keys, f"no button missing: {keys}"

    st.click("slib_del_yes_btn")
    _rerender(st, page_skills_library)
    assert deleted == ["abc123"]


def test_orchestrator_delete_requires_confirmation(ui_env, monkeypatch):
    orch = {
        "slug": "custom1", "name": "Custom Employee", "description": "",
        "is_builtin": False, "tools": [], "sort_order": 100,
    }
    deleted = []
    import core.orchestrators as orchs_mod
    monkeypatch.setattr(orchs_mod, "list_orchestrators", lambda: [orch])
    monkeypatch.setattr(orchs_mod, "delete_orchestrator",
                        lambda slug: deleted.append(slug) or True)

    from ui.pages.orchestrators import page_orchestrators

    st = ui_env
    st.session_state.update({"ui_lang": "English"})
    _rerender(st, page_orchestrators)

    st.click("orch_mgmt_del_custom1")
    _rerender(st, page_orchestrators)
    st.reset_clicks()
    _rerender(st, page_orchestrators)

    assert deleted == [], "orchestrator delete fired without confirmation"
    assert any("Deleting" in str(w) for w in st.warnings), \
        f"no confirmation warning: {st.warnings}"
    keys = _button_keys(st)
    assert "orch_mgmt_del_yes_custom1" in keys, f"yes button missing: {keys}"
    assert "orch_mgmt_del_no_custom1" in keys, f"no button missing: {keys}"

    st.click("orch_mgmt_del_yes_custom1")
    _rerender(st, page_orchestrators)
    assert deleted == ["custom1"]


# ─── neutral employee caption ────────────────────────────────────────────────

def _render_orch_chat_with_employee(st, monkeypatch, orch_dict):
    """Render the employee chat tab with a mocked employee record.

    ``ui.pages.orchestrator`` imports ``get_orchestrator`` at module import
    time, so the page module's own name must be patched (patching
    ``core.orchestrators.get_orchestrator`` would not affect it).
    """
    import ui.pages.orchestrator as orch_page
    monkeypatch.setattr(orch_page, "get_orchestrator",
                        lambda slug: orch_dict if slug == "custom1" else None)
    monkeypatch.setattr(orch_page, "_assistant_has_api_key", lambda svc: True)
    _rerender(st, lambda: orch_page.page_orchestrator("custom1"))


def test_orchestrator_caption_uses_employee_description(ui_env, monkeypatch):
    """The legacy 'developer assistant' caption must be gone."""
    orch = {
        "slug": "custom1", "name": "Marketer",
        "description": "Helps with marketing campaigns",
        "is_builtin": False, "prompt_text": "", "config": {
            "strong_service": "DeepSeek", "strong_model": "m1"},
    }
    _render_orch_chat_with_employee(ui_env, monkeypatch, orch)
    caps = _captions(ui_env)
    assert "Helps with marketing campaigns" in caps, f"captions: {caps}"
    assert not any("developer" in str(c).lower() for c in caps), f"captions: {caps}"


def test_orchestrator_caption_falls_back_to_name(ui_env, monkeypatch):
    """Without a description the caption is just the employee name."""
    orch = {
        "slug": "custom1", "name": "Marketer", "description": "",
        "is_builtin": False, "prompt_text": "", "config": {
            "strong_service": "DeepSeek", "strong_model": "m1"},
    }
    _render_orch_chat_with_employee(ui_env, monkeypatch, orch)
    caps = _captions(ui_env)
    assert "Marketer" in caps, f"captions: {caps}"
    assert not any("developer" in str(c).lower() for c in caps), f"captions: {caps}"


# ─── Step 8: saved-model availability warnings ───────────────────────────────


def _models_settings_warn_env(ui_env, monkeypatch, orch_config, services):
    import ui.pages.orchestrator as orch_mod

    orch_mod.st = ui_env
    orch = {"slug": "o1", "prompt_text": "", "config": orch_config}
    monkeypatch.setattr(orch_mod, "get_orchestrator", lambda slug: orch)
    monkeypatch.setattr(orch_mod, "get_services", lambda: services)
    monkeypatch.setattr(orch_mod, "t", lambda key, *a, **k: key)
    try:
        _rerender(ui_env, lambda: orch_mod._render_models_settings("o1", "English"))
    except Exception as exc:  # pragma: no cover - diagnostic only
        raise AssertionError(f"_render_models_settings crashed: {exc}")
    return ui_env


def _svc(models):
    return {
        "auth_type": "bearer",
        "base_url": "https://mock",
        "config_key": "k",
        "models": [{"id": m} for m in models],
        "temp_min": 0, "temp_max": 1, "temp_step": 0.1,
        "max_tokens_default": 65536,
    }


def test_orch_models_settings_warns_unavailable_service(ui_env, monkeypatch):
    """A saved provider that no longer exists must trigger a warning."""
    ui_env = _models_settings_warn_env(
        ui_env, monkeypatch,
        orch_config={
            "strong_service": "Gone", "strong_model": "m1",
            "weak_service": "Svc", "weak_model": "m1",
            "search_service": "Svc", "search_model": "m1",
        },
        services={"Svc": _svc(["m1"])},
    )
    assert any("orch_service_unavailable" in str(w) for w in ui_env.warnings), (
        f"expected orch_service_unavailable warning, got: {ui_env.warnings}"
    )
    assert not any("orch_model_unavailable" in str(w) for w in ui_env.warnings)


def test_orch_models_settings_warns_unavailable_model(ui_env, monkeypatch):
    """A saved model missing from the current provider must trigger a warning."""
    ui_env = _models_settings_warn_env(
        ui_env, monkeypatch,
        orch_config={
            "strong_service": "Svc", "strong_model": "gone_m",
            "weak_service": "Svc", "weak_model": "m1",
            "search_service": "Svc", "search_model": "m1",
        },
        services={"Svc": _svc(["m1"])},
    )
    assert any("orch_model_unavailable" in str(w) for w in ui_env.warnings), (
        f"expected orch_model_unavailable warning, got: {ui_env.warnings}"
    )


def test_orch_models_settings_no_warning_when_all_available(ui_env, monkeypatch):
    """Valid saved models must NOT produce availability warnings."""
    ui_env = _models_settings_warn_env(
        ui_env, monkeypatch,
        orch_config={
            "strong_service": "Svc", "strong_model": "m1",
            "weak_service": "Svc", "weak_model": "m1",
            "search_service": "Svc", "search_model": "m1",
        },
        services={"Svc": _svc(["m1"])},
    )
    assert not any("unavailable" in str(w) for w in ui_env.warnings), (
        f"no availability warnings expected, got: {ui_env.warnings}"
    )


def test_assistant_chat_warns_unavailable_saved_provider(ui_env, monkeypatch):
    """The assistant chat page warns when the saved provider is gone."""
    import ui.pages.chat as chat_mod

    chat_mod.st = ui_env
    index_entry = {
        "id": "a1", "name": "T", "service": "Gone", "model": "m",
        "temperature": 0.5, "created_at": "2026-01-01T00:00:00",
        "tools": [], "max_tool_calls": None,
    }
    assistant = dict(index_entry, text="prompt", max_tokens=None)
    ui_env.session_state.update({
        "ui_lang": "English",
        "selected_assistant_id": "a1",
        "selected_skill_id": "a1",
        "active_thread_id": None,
        "attached_file_context": "",
        "attached_file_name": "",
        "force_send": False,
        "input_key": 0,
        "user_input_value": "",
    })
    monkeypatch.setattr(chat_mod, "load_assistants_index", lambda: [index_entry])
    monkeypatch.setattr(chat_mod, "get_assistant_by_id",
                        lambda aid: assistant if aid == "a1" else None)
    monkeypatch.setattr(chat_mod, "get_services", lambda: {})
    monkeypatch.setattr(chat_mod, "record_assistant_use", lambda sid: None)
    monkeypatch.setattr(chat_mod, "list_assistant_files", lambda aid: [])
    monkeypatch.setattr(chat_mod, "load_assistant_files_context", lambda *a, **k: "")
    monkeypatch.setattr(chat_mod, "load_thread_messages", lambda tid: [])
    monkeypatch.setattr(chat_mod, "check_context",
                        lambda *a, **k: {"ok": True, "total_tokens": 10,
                                         "limit": 1000, "excess_chars": 0})
    monkeypatch.setattr(chat_mod, "get_file_uploader_types", lambda: [])
    monkeypatch.setattr(chat_mod, "check_upload_tokens", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(chat_mod, "t", lambda key, *a, **k: key)

    _rerender(ui_env, chat_mod.page_run_query)
    assert any("assistant_service_unavailable" in str(w) for w in ui_env.warnings), (
        f"expected assistant_service_unavailable warning, got: {ui_env.warnings}"
    )


def test_assistant_chat_warns_unavailable_saved_model(ui_env, monkeypatch):
    """The assistant chat page warns when the saved model is gone."""
    import ui.pages.chat as chat_mod

    chat_mod.st = ui_env
    index_entry = {
        "id": "a1", "name": "T", "service": "Svc", "model": "gone_m",
        "temperature": 0.5, "created_at": "2026-01-01T00:00:00",
        "tools": [], "max_tool_calls": None,
    }
    assistant = dict(index_entry, text="prompt", max_tokens=None)
    ui_env.session_state.update({
        "ui_lang": "English",
        "selected_assistant_id": "a1",
        "selected_skill_id": "a1",
        "active_thread_id": None,
        "attached_file_context": "",
        "attached_file_name": "",
        "force_send": False,
        "input_key": 0,
        "user_input_value": "",
    })
    monkeypatch.setattr(chat_mod, "load_assistants_index", lambda: [index_entry])
    monkeypatch.setattr(chat_mod, "get_assistant_by_id",
                        lambda aid: assistant if aid == "a1" else None)
    monkeypatch.setattr(chat_mod, "get_services", lambda: {"Svc": _svc(["m1"])})
    monkeypatch.setattr(chat_mod, "record_assistant_use", lambda sid: None)
    monkeypatch.setattr(chat_mod, "list_assistant_files", lambda aid: [])
    monkeypatch.setattr(chat_mod, "load_assistant_files_context", lambda *a, **k: "")
    monkeypatch.setattr(chat_mod, "load_thread_messages", lambda tid: [])
    monkeypatch.setattr(chat_mod, "check_context",
                        lambda *a, **k: {"ok": True, "total_tokens": 10,
                                         "limit": 1000, "excess_chars": 0})
    monkeypatch.setattr(chat_mod, "get_file_uploader_types", lambda: [])
    monkeypatch.setattr(chat_mod, "check_upload_tokens", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(chat_mod, "t", lambda key, *a, **k: key)

    _rerender(ui_env, chat_mod.page_run_query)
    assert any("assistant_model_unavailable" in str(w) for w in ui_env.warnings), (
        f"expected assistant_model_unavailable warning, got: {ui_env.warnings}"
    )


# ─── Step 9: sidebar search reset and 'Providers' label ───────────────


def _sidebar_app(ui_env, monkeypatch):
    import ui.app as app_mod

    monkeypatch.setattr(app_mod, "load_assistants_index",
                        lambda: [{
                            "id": "a1", "name": "Test Assistant",
                            "service": "Svc", "model": "m",
                            "temperature": 0.5,
                            "created_at": "2026-01-01T00:00:00",
                            "tools": [], "max_tool_calls": None,
                        }])
    monkeypatch.setattr(app_mod, "list_chat_threads", lambda: [])
    monkeypatch.setattr(app_mod, "list_orchestrators", lambda: [])
    ui_env.session_state.update({
        "ui_lang": "English",
        "current_page": "welcome",
        "_defaults_seeded": True,
        "assistant_search_query": "Test",
        "skill_search_query": "Test",
        "assistant_search_reset": 0,
        "recent_assistant_ids": [],
        "recent_skill_ids": [],
        "selected_assistant_id": None,
        "selected_skill_id": None,
    })
    return app_mod


def test_sidebar_assistant_click_resets_search(ui_env, monkeypatch):
    """Selecting an assistant from the search results must clear the query."""
    app_mod = _sidebar_app(ui_env, monkeypatch)
    _rerender(ui_env, app_mod.main)

    button_keys = [
        kwargs.get("key")
        for name, _, kwargs in ui_env.calls
        if name == "button"
    ]
    assert "nav_assistant_a1" in button_keys, f"btn missing: {button_keys}"

    ui_env.reset_clicks()
    ui_env.click("nav_assistant_a1")
    try:
        app_mod.main()
    except StopRerun:
        pass  # expected: the handler reruns

    assert ui_env.session_state.get("assistant_search_query") == ""
    assert ui_env.session_state.get("skill_search_query") == ""
    assert ui_env.session_state.get("assistant_search_reset") == 1


def test_sidebar_settings_providers_label_updated(ui_env, monkeypatch):
    """The LLM-providers nav item is labelled with the 'Providers' key."""
    import json
    from pathlib import Path

    app_mod = _sidebar_app(ui_env, monkeypatch)
    en_path = Path(app_mod.__file__).resolve().parent.parent / \
        "defaults" / "langs" / "en.json"
    with open(en_path, encoding="utf-8") as fh:
        en = json.load(fh)
    monkeypatch.setattr(app_mod, "t",
                        lambda key, lang=None, **k: en.get(key, key).format(**k))

    _rerender(ui_env, app_mod.main)

    nav_labels = [
        args[0]
        for name, args, kwargs in ui_env.calls
        if name == "button" and kwargs.get("key") == "nav_settings"
    ]
    assert nav_labels, "nav_settings button not rendered"
    assert "Providers" in nav_labels[0], f"label: {nav_labels[0]}"



# ─── Step 9b: stale assistant prompt-keys cleanup on form switch ─────────────

def test_assistant_form_switch_clears_stale_prompt_keys(ui_env, monkeypatch):
    """Opening the edit form for a different assistant must drop stale
    assistant_prompt_text_* session keys and reset the prompt revision."""
    import core.services as services_mod
    import core.assistants as core_assistants

    svc = {
        "auth_type": "bearer",
        "base_url": "https://mock",
        "config_key": "k",
        "models": [{"id": "m1"}],
        "temp_min": 0, "temp_max": 1, "temp_step": 0.1,
        "max_tokens_default": 65536,
    }

    def _assistant(aid, name, slug):
        return {
            "id": aid, "name": name, "slug": slug,
            "service": "Svc", "model": "m1", "temperature": 0.7,
            "description": "", "text": "prompt",
            "tools": [], "max_tool_calls": None, "max_tokens": None,
            "reasoning_effort": None,
            "created_at": "2026-01-01T00:00:00",
        }

    assistants = {
        "a1": _assistant("a1", "First", "first_assistant"),
        "a2": _assistant("a2", "Second", "second_assistant"),
    }
    monkeypatch.setattr(services_mod, "get_services", lambda: {"Svc": svc})
    monkeypatch.setattr(core_assistants, "get_assistant_by_id",
                        lambda aid: assistants.get(aid))

    # Import the page module AFTER patching core.services so it binds the
    # patched get_services at module import time.
    import ui.pages.assistants as assistants_mod

    monkeypatch.setattr(assistants_mod, "list_tool_definitions", lambda: [])
    monkeypatch.setattr(assistants_mod, "list_rag_bases", lambda: [])

    st = ui_env
    st.session_state.update({
        "ui_lang": "English",
        "show_assistant_form": True,
        "show_skill_form": True,
        "edit_assistant_id": "a1",
        "edit_skill_id": "a1",
        "assistant_prompt_revision_for": "old_assistant",
        "assistant_prompt_revision": 7,
        "assistant_prompt_text_7": "stale draft",
        "assistant_prompt_text_3": "older stale draft",
    })

    _rerender(st, assistants_mod.page_assistants)
    assert "assistant_prompt_text_7" not in st.session_state
    assert "assistant_prompt_text_3" not in st.session_state
    assert st.session_state.get("assistant_prompt_revision") == 0
    assert st.session_state.get("assistant_prompt_revision_for") == "a1"

    # Simulate what Streamlit leaves behind after the first form render, then
    # switch to another assistant: the cleanup must run again.
    st.session_state["assistant_prompt_text_0"] = "draft left by widget"
    st.session_state["edit_assistant_id"] = "a2"
    st.session_state["edit_skill_id"] = "a2"
    _rerender(st, assistants_mod.page_assistants)
    assert "assistant_prompt_text_0" not in st.session_state
    assert st.session_state.get("assistant_prompt_revision") == 0
    assert st.session_state.get("assistant_prompt_revision_for") == "a2"


# ─── orchestrator chat: datetime captions & empty-JSON cleanup ──────────────

def _orch_chat_env(ui_env, monkeypatch, history):
    """Seed session state and patch dependencies for _render_chat_tab."""
    import ui.pages.orchestrator as orch_page

    orch = {
        "slug": "o1", "name": "DevAgent", "description": "",
        "is_builtin": True, "prompt_text": "",
        "config": {"strong_service": "Svc", "strong_model": "m1"},
    }
    monkeypatch.setattr(orch_page, "get_orchestrator", lambda slug: orch)
    monkeypatch.setattr(orch_page, "_assistant_has_api_key", lambda svc: True)
    monkeypatch.setattr(orch_page, "t", lambda key, *a, **k: key)
    monkeypatch.setattr(orch_page, "get_file_uploader_types", lambda: [])
    monkeypatch.setattr(
        orch_page, "build_assistant_dicts",
        lambda slug: ({"service": "Svc", "model": "m1", "temperature": 0.5,
                       "text": "p", "max_tokens": 0}, {}),
    )
    monkeypatch.setattr(
        orch_page, "check_context",
        lambda *a, **k: {"total_tokens": 10, "ok": True,
                         "limit": 1000, "excess_chars": 0},
    )
    monkeypatch.setattr(orch_page, "sum_thread_tokens", lambda msgs: (0, 0, 0))

    ui_env.session_state.update({
        "ui_lang": "English",
        "orch_o1_history": history,
        "orch_o1_loop_state": None,
        "orch_o1_thread_id": None,
        "orch_o1_web_search": False,
        "orch_o1_economy_mode": False,
        "orch_o1_safety_mode": True,
        "orch_o1_attached": [],
        "orch_o1_upload_counter": 0,
        "orch_o1_saved_msg_count": len(history),
        "orch_o1_dispatcher": None,
        "orch_o1_scroll_to": None,
    })
    # Keep the mock chat_input empty so the send path never triggers.
    ui_env.chat_input = lambda *a, **k: ""
    return orch_page


def test_orch_chat_renders_datetime_captions(ui_env, monkeypatch):
    """Message timestamps render as 🕐 HH:MM DD.MM.YYYY under both roles."""
    orch_page = _orch_chat_env(ui_env, monkeypatch, [
        {"role": "user", "content": "Hi", "ts": "2026-08-23T09:30:49.100474"},
        {"role": "assistant", "content": "Done.", "ts": "2026-08-23T09:31:02"},
    ])
    _rerender(ui_env, lambda: orch_page._render_chat_tab("o1", "English"))
    caps = _captions(ui_env)
    assert "🕐 09:30 23.08.2026" in caps, f"captions: {caps}"
    assert "🕐 09:31 23.08.2026" in caps, f"captions: {caps}"
    # The legacy time-only format must be gone.
    assert not any(c == "🕐 09:30" or c == "🕐 09:31" for c in caps), \
        f"time-only captions still present: {caps}"


def test_orch_chat_omits_caption_without_ts(ui_env, monkeypatch):
    """Messages without a ts do not render a 🕐 caption."""
    orch_page = _orch_chat_env(ui_env, monkeypatch, [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Done."},
    ])
    _rerender(ui_env, lambda: orch_page._render_chat_tab("o1", "English"))
    caps = _captions(ui_env)
    assert not any("🕐" in str(c) for c in caps), f"captions: {caps}"


def test_orchestrator_strips_empty_json_fences(ui_env):
    """Empty/whitespace fenced blocks and glued 'json' residue must not leak
    into the displayed chat text."""
    from ui.pages.orchestrator import _strip_tool_calls

    assert _strip_tool_calls("```json\n```") == ""
    assert _strip_tool_calls("```json\n\n```") == ""
    assert _strip_tool_calls("Текст.\n\n```json\n\n```\n\nДальше.") == \
        "Текст.\n\nДальше."

    residue = "```json\n\njson\n\njson\n\njson\n\n```"
    assert _strip_tool_calls(residue) == "", repr(_strip_tool_calls(residue))
    assert _strip_tool_calls("Перед.\n\n" + residue + "\n\nПосле.") == \
        "Перед.\n\nПосле."

    real_code = "```python\nprint(1)\n```"
    assert _strip_tool_calls("Код:\n\n" + real_code) == "Код:\n\n" + real_code

    # Consecutive tool-call blocks must leave no stray 'json' lines either.
    tool_blocks = (
        "```json\n{\"tool\": \"read_file\", \"args\": {\"path\": \"a\"}}\n```\n"
        "```json\n{\"tool\": \"read_file\", \"args\": {\"path\": \"b\"}}\n```"
    )
    assert _strip_tool_calls(tool_blocks) == "", repr(_strip_tool_calls(tool_blocks))


def _assistant_chat_env(ui_env, monkeypatch, messages):
    """Set up the assistant chat page with a thread of the given messages."""
    import ui.pages.chat as chat_mod

    chat_mod.st = ui_env
    index_entry = {
        "id": "a1", "name": "T", "service": "Svc", "model": "m1",
        "temperature": 0.5, "created_at": "2026-01-01T00:00:00",
        "tools": [], "max_tool_calls": None,
    }
    assistant = dict(index_entry, text="prompt", max_tokens=None)
    ui_env.session_state.update({
        "ui_lang": "English",
        "selected_assistant_id": "a1",
        "selected_skill_id": "a1",
        "active_thread_id": "t1",
        "attached_file_context": "",
        "attached_file_name": "",
        "force_send": False,
        "input_key": 0,
    })
    monkeypatch.setattr(chat_mod, "load_assistants_index", lambda: [index_entry])
    monkeypatch.setattr(chat_mod, "get_assistant_by_id",
                        lambda aid: assistant if aid == "a1" else None)
    monkeypatch.setattr(chat_mod, "get_services", lambda: {"Svc": _svc(["m1"])})
    monkeypatch.setattr(chat_mod, "record_assistant_use", lambda sid: None)
    monkeypatch.setattr(chat_mod, "list_assistant_files", lambda aid: [])
    monkeypatch.setattr(chat_mod, "load_assistant_files_context", lambda *a, **k: "")
    monkeypatch.setattr(chat_mod, "load_thread_meta", lambda tid: {
        "assistant_id": "a1", "assistant_name": "T",
        "created_at": "2026-01-01T00:00:00", "title": "Thread",
    })
    monkeypatch.setattr(chat_mod, "load_thread_messages", lambda tid: messages)
    monkeypatch.setattr(chat_mod, "sum_thread_tokens", lambda msgs: (0, 0, 0))
    monkeypatch.setattr(chat_mod, "check_context",
                        lambda *a, **k: {"ok": True, "total_tokens": 10,
                                         "limit": 1000, "excess_chars": 0})
    monkeypatch.setattr(chat_mod, "get_file_uploader_types", lambda: [])
    monkeypatch.setattr(chat_mod, "check_upload_tokens", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(chat_mod, "t", lambda key, *a, **k: key)
    return chat_mod


def test_assistant_chat_renders_datetime_captions(ui_env, monkeypatch):
    """Assistant chat message timestamps render as \U0001f550 HH:MM DD.MM.YYYY."""
    messages = [
        {"role": "user", "content": "Hi", "ts": "2026-08-23T09:30:49.100474"},
        {"role": "assistant", "content": "Done.", "ts": "2026-08-23T09:31:02"},
    ]
    chat_mod = _assistant_chat_env(ui_env, monkeypatch, messages)
    _rerender(ui_env, chat_mod.page_run_query)
    caps = _captions(ui_env)
    assert "\U0001f550 09:30 23.08.2026" in caps, f"captions: {caps}"
    assert "\U0001f550 09:31 23.08.2026" in caps, f"captions: {caps}"
    # The legacy time-only format must be gone.
    assert not any(c == "\U0001f550 09:30" or c == "\U0001f550 09:31" for c in caps), \
        f"time-only captions still present: {caps}"


def test_assistant_chat_omits_caption_without_ts(ui_env, monkeypatch):
    """Assistant chat messages without a ts do not render a \U0001f550 caption."""
    messages = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Done."},
    ]
    chat_mod = _assistant_chat_env(ui_env, monkeypatch, messages)
    _rerender(ui_env, chat_mod.page_run_query)
    caps = _captions(ui_env)
    assert not any("\U0001f550" in str(c) for c in caps), f"captions: {caps}"


# ─── orchestrator chat: upload processing must not block sending ───────────


class _FakeUpload:
    """Minimal uploaded-file stand-in exposing just the .name attribute."""

    def __init__(self, name):
        self.name = name


def _orch_upload_env(ui_env, monkeypatch, uploaded, attached=None):
    """Seed the chat tab and patch the upload-processing helpers.

    ``uploaded`` is a list of file names the mocked uploader returns on
    every render; ``attached`` is the initial attached-files list.
    """
    orch_page = _orch_chat_env(ui_env, monkeypatch, [])
    monkeypatch.setattr(orch_page, "extract_file_content",
                        lambda uf: "file content")
    monkeypatch.setattr(orch_page, "check_upload_tokens",
                        lambda content: (True, 5))
    monkeypatch.setattr(orch_page, "build_attachment_metadata",
                        lambda name, content: {"name": name,
                                               "content": content})
    ui_env.session_state["orch_o1_attached"] = list(attached or [])

    def _fake_uploader(*a, **k):
        ui_env._rec("file_uploader", a, k)
        return [_FakeUpload(n) for n in uploaded]

    ui_env.file_uploader = _fake_uploader
    return orch_page


def test_orch_upload_attaches_file_and_bumps_uploader_key(ui_env, monkeypatch):
    """Uploading a file must add it to the attachment list, bump the reset
    counter (so the uploader widget key changes) and rerun exactly once."""
    orch_page = _orch_upload_env(ui_env, monkeypatch, uploaded=["report.txt"])
    _rerender(ui_env, lambda: orch_page._render_chat_tab("o1", "English"))

    attached = ui_env.session_state["orch_o1_attached"]
    assert [f["name"] for f in attached] == ["report.txt"], attached
    assert ui_env.session_state["orch_o1_upload_counter"] == 1
    assert ui_env.rerun_count == 1

    keys = [c[2].get("key") for c in ui_env.calls if c[0] == "file_uploader"]
    assert "orch_upload_o1_0" in keys, keys


def test_orch_upload_second_render_settles_without_refire(ui_env, monkeypatch):
    """The upload processing must not loop forever: the second render (with
    the same file still reported by a stale widget) attaches nothing new,
    bumps the key again and does NOT rerun - the chat stays usable.

    In real Streamlit the recreated widget returns no files at all; the
    mock keeps returning the file to prove the guard works even in the
    worst case.
    """
    orch_page = _orch_upload_env(ui_env, monkeypatch, uploaded=["report.txt"])
    _rerender(ui_env, lambda: orch_page._render_chat_tab("o1", "English"))
    assert ui_env.rerun_count == 1

    _rerender(ui_env, lambda: orch_page._render_chat_tab("o1", "English"))
    assert ui_env.rerun_count == 1, (
        "chat page keeps rerunning on every render - sending stays blocked"
    )
    attached = ui_env.session_state["orch_o1_attached"]
    assert [f["name"] for f in attached] == ["report.txt"]
    assert ui_env.session_state["orch_o1_upload_counter"] == 2
    keys = [c[2].get("key") for c in ui_env.calls if c[0] == "file_uploader"]
    assert "orch_upload_o1_1" in keys, keys


def test_orch_upload_too_large_shows_error_without_rerun_loop(ui_env, monkeypatch):
    """A file over the token limit must show the error and reset the widget
    without entering a rerun loop."""
    orch_page = _orch_chat_env(ui_env, monkeypatch, [])
    monkeypatch.setattr(orch_page, "extract_file_content", lambda uf: "big")
    monkeypatch.setattr(orch_page, "check_upload_tokens",
                        lambda content: (False, 999))

    def _fake_uploader(*a, **k):
        ui_env._rec("file_uploader", a, k)
        return [_FakeUpload("big.txt")]

    ui_env.file_uploader = _fake_uploader

    _rerender(ui_env, lambda: orch_page._render_chat_tab("o1", "English"))
    assert ui_env.rerun_count == 0
    assert any("file_too_large_tokens" in str(e) for e in ui_env.errors), \
        ui_env.errors
    assert ui_env.session_state["orch_o1_attached"] == []
    assert ui_env.session_state["orch_o1_upload_counter"] == 1


def test_orch_upload_duplicate_is_ignored_without_rerun_loop(ui_env, monkeypatch):
    """Re-selecting an already attached file must not duplicate it and must
    not trigger repeated reruns."""
    orch_page = _orch_upload_env(
        ui_env, monkeypatch, uploaded=["report.txt"],
        attached=[{"name": "report.txt", "content": "old"}],
    )
    _rerender(ui_env, lambda: orch_page._render_chat_tab("o1", "English"))

    attached = ui_env.session_state["orch_o1_attached"]
    assert len(attached) == 1, attached
    assert attached[0]["content"] == "old"
    assert ui_env.rerun_count == 0
    assert ui_env.session_state["orch_o1_upload_counter"] == 1