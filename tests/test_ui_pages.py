"""
UI-page tests for SagaAI - smoke tests that exercise page render functions
under a mocked Streamlit environment. They verify that pages do not crash
and that key UI elements (buttons, forms, lists) are rendered.

Some click-driven tests are included by pre-seeding the Streamlit mock
with the relevant widget key and then asserting the resulting rendering
side effects (for example, a success notification after saving a provider).
"""

from __future__ import annotations

import os
import sys
import pytest
from contextlib import ExitStack
from unittest.mock import patch

from tests._st_mock import install_streamlit_mock, StopRerun


# ── bundles of patches to stay within Python's block-nesting limit ────────────

PATCHES_CORE          = [
    ("core.i18n.get_langs",              {"English": "en.json", "Русский": "ru.json"}),
    ("core.i18n.t",                      None),
    ("core.config.load_config",          {}),
    ("core.config.save_config",          None),
    ("core.config.has_key",              True),
    ("core.config.is_env_key_set_for_service", False),
    ("core.config.load_devagent_config", {}),
    ("core.config.save_devagent_config", None),
    ("core.config.list_env_keys",        {}),
]

PATCHES_API = [
    ("core.api_layer.test_connection",   (True, "OK")),
    ("core.services.get_services",       {}),
]

PATCHES_SKILLS = [
    ("core.skills.load_prompts_index",   []),
    ("core.skills.get_prompt_by_id",     None),
    ("core.skills.create_skill",         "new_id"),
    ("core.skills.update_skill",         None),
    ("core.skills.delete_skill",         None),
    ("core.skills.list_skill_files",     []),
    ("core.skills.save_skill_file",      None),
    ("core.skills.delete_skill_file",    None),
]

PATCHES_MISC = [
    ("core.threads.load_thread_meta",            {}),
    ("core.default_imports.ensure_all_defaults", None),
    ("core.env_loader.load_env_from_shell_profiles", None),
    ("core.files.ensure_optional_dependencies",  []),
    ("core.files.get_file_uploader_types",       []),
]

PATCHES_INSTRUCTIONS = [
    ("core.instructions.list_instructions",     []),
    ("core.instructions.get_instruction",       None),
    ("core.instructions.create_instruction",    None),
    ("core.instructions.update_instruction",    None),
    ("core.instructions.delete_instruction",    None),
]


SAMPLE_SERVICE = {
    "auth_type": "bearer",
    "base_url": "https://mock",
    "config_key": "test_key",
    "models": [{"id": "m1"}],
    "temp_default": 0.7,
    "extra_fields": [],
}


def _apply_all(bundles):
    """Return an ExitStack whose enter() applies all patches in every bundle."""
    stack = ExitStack()
    for bundle in bundles:
        for target, ret in bundle:
            if ret is None:
                stack.enter_context(patch(target))
            else:
                stack.enter_context(patch(target, return_value=ret))
    return stack


# ── helper ────────────────────────────────────────────────────────────────────
def invoke_page(page_fn, **session_state):
    """Call *page_fn* inside a try/except that swallows StopRerun."""
    import streamlit as st
    for k, v in session_state.items():
        st.session_state[k] = v
    try:
        page_fn()
    except StopRerun:
        pass


def _call_names(st_mock):
    """Return only the recorded call method names."""
    return [name for name, _, _ in st_mock.calls]


# ── main fixture ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_env():
    """Install Streamlit mock + core dependency mocks for every test."""
    # Drop ui.* modules so pages re-import under the mock, even if they were
    # previously imported with the REAL streamlit by other test files
    # (e.g. tests/smoke/test_app_smoke.py).
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as st_mock:
        st_mock.session_state.clear()
        st_mock.session_state.update({
            "ui_lang": "English",
            "current_page": "run",
        })
        with _apply_all([PATCHES_CORE,
                         PATCHES_API,
                         PATCHES_SKILLS,
                         PATCHES_MISC,
                         PATCHES_INSTRUCTIONS]):
            yield st_mock


# ─── ui.app main ──────────────────────────────────────────────────────────────

def test_main_base_renders():
    # ui.* modules are re-imported under the active mock by mock_env.
    from ui.app import main
    invoke_page(main)


def test_theme_selector_rendered_in_sidebar(mock_env):
    """The sidebar Settings section renders a theme selectbox with the three
    native Streamlit theme modes."""
    from ui.app import main
    invoke_page(main)

    selectbox_calls = [
        (args, kwargs)
        for name, args, kwargs in mock_env.calls
        if name == "selectbox" and kwargs.get("key") == "theme_selector"
    ]
    assert len(selectbox_calls) == 1
    _, kwargs = selectbox_calls[0]
    assert kwargs["options"] == ["System", "Light", "Dark"]
    assert kwargs.get("index") == 0
    assert "format_func" in kwargs
    assert not mock_env.errors


def _render_theme_select(mock_env, mode):
    """Render main() with the theme selectbox returning *mode*."""
    from ui.app import main
    mock_env._selectbox_returns["theme_selector"] = mode
    invoke_page(main)
    return [c for c in mock_env.calls if c[0] == "html"]


def test_theme_dark_select_writes_native_streamlit_theme_key(mock_env):
    """Selecting 'Dark' must emit an st.html(unsafe_allow_javascript=True)
    call that writes the native Streamlit theme key (stActiveTheme-<path>/v2)
    with \"Dark\", attaches the UI-restore snapshot to the URL and replaces
    the page (location.replace) instead of a plain location.reload()."""
    html_calls = _render_theme_select(mock_env, "Dark")

    assert html_calls, "theme select must emit an st.html call"
    payload = html_calls[0][1][0] if html_calls[0][1] else ""
    assert "stActiveTheme-" in payload
    assert "localStorage.setItem" in payload
    assert 'JSON.stringify("Dark")' in payload
    assert "_sagaai_ui_restore" in payload
    assert "location.replace" in payload
    assert "location.reload()" not in payload
    assert html_calls[0][2].get("unsafe_allow_javascript") is True
    assert not mock_env.errors


def test_theme_system_and_light_select_emit_correct_mode(mock_env):
    """System and Light selections must emit \"System\" and \"Light\" payloads."""
    from unittest.mock import patch
    from ui.app import main

    for mode in ("System", "Light"):
        mock_env.reset_clicks()
        mock_env.calls.clear()
        mock_env._selectbox_returns.clear()
        mock_env._selectbox_returns["theme_selector"] = mode
        # Current theme differs, so the new selection must trigger _apply_theme.
        with patch("ui.app.load_config", return_value={"ui_theme": "Dark"}):
            invoke_page(main)

        html_calls = [
            c for c in mock_env.calls if c[0] == "html"
        ]
        assert html_calls, f"{mode} select must emit an st.html call"
        payload = html_calls[0][1][0] if html_calls[0][1] else ""
        expected = f'JSON.stringify("{mode}")'
        assert expected in payload
        assert not mock_env.errors


# ─── ui.pages.skills ──────────────────────────────────────────────────────────

def test_skills_page_when_empty():
    from ui.pages.skills import page_skills
    invoke_page(page_skills)


def test_skills_page_with_skills_list():
    from ui.pages.skills import page_skills
    with patch("core.skills.load_prompts_index") as mock_idx:
        mock_idx.return_value = [{
            "id": "sk1", "name": "Test skill",
            "description": "A test", "service": "Svc",
            "model": "m", "temperature": 0.5,
            "created_at": "2025-01-01T00:00:00",
            "tools": [], "max_tool_calls": None
        }]
        invoke_page(page_skills)


# ─── ui.pages.settings ────────────────────────────────────────────────────────

def test_settings_api_keys_tab_renders():
    from ui.pages.settings import page_settings
    with patch("core.services.get_services", return_value={
        "TestSvc": SAMPLE_SERVICE
    }):
        invoke_page(page_settings)


def test_devagent_settings_tab_renders():
    from ui.pages.settings import page_settings
    with patch("core.services.get_services", return_value={
        "TestSvc": {
            "auth_type": "bearer",
            "base_url": "https://mock",
            "config_key": "test_key",
            "models": [{"id": "m1"}],
            "temp_min": 0, "temp_max": 1, "temp_step": 0.1,
            "tools_options": [{"key": "web_search"}]
        }
    }):
        invoke_page(page_settings)


def test_instructions_tab_renders():
    from ui.pages.settings import page_settings
    with patch("core.instructions.list_instructions", return_value=[]):
        invoke_page(page_settings)


def test_settings_provider_save_shows_success(mock_env):
    """Saving a provider form should render a success notification in place."""
    from ui.pages.settings import page_settings
    import ui.pages.settings as settings_mod

    # Ensure the cached module uses the current Streamlit mock.
    settings_mod.st = mock_env

    with patch("ui.pages.settings.load_config", return_value={}), \
         patch("ui.pages.settings.get_services", return_value={"TestSvc": SAMPLE_SERVICE}), \
         patch("ui.pages.settings.save_config", return_value=True), \
         patch("ui.pages.settings.has_key", return_value=False), \
         patch("ui.pages.settings.is_env_key_set_for_service", return_value=False):
        mock_env.click("settings_save_TestSvc")
        invoke_page(page_settings)

    assert "success" in _call_names(mock_env)
    # No flash flag should be used anymore; confirmation is rendered directly.
    assert "settings_saved_msg" not in mock_env.session_state


def test_settings_global_save_button_absent(mock_env):
    """The global Save button under provider blocks must be gone."""
    from ui.pages.settings import page_settings
    import ui.pages.settings as settings_mod

    settings_mod.st = mock_env

    with patch("ui.pages.settings.load_config", return_value={}), \
         patch("ui.pages.settings.get_services", return_value={"TestSvc": SAMPLE_SERVICE}), \
         patch("ui.pages.settings.has_key", return_value=False), \
         patch("ui.pages.settings.is_env_key_set_for_service", return_value=False):
        invoke_page(page_settings)

    button_keys = [
        kwargs.get("key")
        for name, args, kwargs in mock_env.calls
        if name in ("button", "form_submit_button")
    ]
    assert "settings_save_btn_global" not in button_keys


# ─── ui.components.workspace_picker ───────────────────────────────────────────

def test_workspace_picker_initial_state():
    from ui.components.workspace_picker import render_workspace_picker
    invoke_page(lambda: render_workspace_picker("test", lang="English"))

# ─── orchestrator favicon: single static SVG only ──────────────────────────────

def test_orchestrator_favicon_is_static_only(mock_env):
    """The dynamic favicon switcher is removed.

    The orchestrator module must no longer define ``_apply_favicon``, and the
    app entrypoint must keep one static ``assets/favicon.svg`` for every OS.
    """
    import ui.pages.orchestrator as orch_mod

    assert not hasattr(orch_mod, "_apply_favicon")

    import os
    app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
    with open(app_path, "r", encoding="utf-8") as fh:
        app_src = fh.read()
    assert 'page_icon=os.path.join(_project_root, "assets", "favicon.svg")' in app_src
    assert "_apply_favicon" not in app_src
    assert os.path.exists(os.path.join(os.path.dirname(__file__), "..", "assets", "favicon.svg"))

# ─── orchestrator models settings: web_search_prompt textarea ───────────────

def test_orchestrator_models_settings_renders_search_prompt_area(mock_env):
    """The models-settings renderer must show the search-agent prompt textarea
    and persist its value into the orchestrator config on save."""
    from ui.pages.orchestrator import _render_models_settings
    import ui.pages.orchestrator as orch_mod

    orch_mod.st = mock_env

    services = {
        "Svc": {
            "auth_type": "bearer",
            "base_url": "https://mock",
            "config_key": "k",
            "models": [{"id": "m1"}, {"id": "m2"}],
            "temp_min": 0, "temp_max": 1, "temp_step": 0.1,
            "tools_options": [{"key": "web_search"}],
            "max_tokens_default": 65536,
        }
    }
    orch = {
        "config": {
            "strong_service": "Svc",
            "strong_model": "m1",
            "weak_service": "Svc",
            "weak_model": "m1",
            "search_service": "Svc",
            "search_model": "m1",
            "web_search_prompt": "Custom base prompt",
        },
        "prompt_text": "",
    }

    with patch("ui.pages.orchestrator.get_orchestrator", return_value=orch), \
         patch("ui.pages.orchestrator.get_services", return_value=services), \
         patch("ui.pages.orchestrator.save_orchestrator", return_value=True) as mock_save:
        invoke_page(lambda: _render_models_settings("myorch", "English"))

    text_area_keys = [
        kwargs.get("key")
        for name, _, kwargs in mock_env.calls
        if name == "text_area"
    ]
    assert "orch_set_search_prompt_myorch" in text_area_keys

    # Re-render with the Save button clicked and verify persistence.
    mock_env.reset_clicks()
    mock_env.click("orch_save_models_myorch")
    with patch("ui.pages.orchestrator.get_orchestrator", return_value=orch), \
         patch("ui.pages.orchestrator.get_services", return_value=services), \
         patch("ui.pages.orchestrator.save_orchestrator", return_value=True) as mock_save:
        invoke_page(lambda: _render_models_settings("myorch", "English"))

    saved_cfg = mock_save.call_args.kwargs["config"]
    assert saved_cfg["web_search_prompt"] == "Custom base prompt"


def test_orchestrator_models_settings_saves_reasoning_effort(mock_env):
    """Reasoning-effort selects persist strong/weak/search values into config."""
    from ui.pages.orchestrator import _render_models_settings
    import ui.pages.orchestrator as orch_mod

    orch_mod.st = mock_env

    re_svc = {
        "auth_type": "bearer",
        "base_url": "https://mock",
        "config_key": "k",
        "models": [{"id": "m1"}, {"id": "m2"}],
        "temp_min": 0, "temp_max": 1, "temp_step": 0.1,
        "tools_options": [{"key": "web_search"}],
        "max_tokens_default": 65536,
        "extra_fields": [{
            "key": "reasoning_effort",
            "type": "select",
            "options": ["none", "low", "medium", "high", "max"],
            "default": "max",
        }],
    }
    orch = {
        "config": {
            "strong_service": "Svc", "strong_model": "m1",
            "weak_service": "Svc", "weak_model": "m1",
            "search_service": "Svc", "search_model": "m1",
        },
        "prompt_text": "",
    }

    with patch("ui.pages.orchestrator.get_orchestrator", return_value=orch), \
         patch("ui.pages.orchestrator.get_services", return_value={"Svc": re_svc}), \
         patch("ui.pages.orchestrator.save_orchestrator", return_value=True) as mock_save:
        mock_env.click("orch_save_models_myorch")
        invoke_page(lambda: _render_models_settings("myorch", "English"))

    saved_cfg = mock_save.call_args.kwargs["config"]
    assert saved_cfg["strong_reasoning_effort"] == "max"
    assert saved_cfg["weak_reasoning_effort"] == "high"
    assert saved_cfg["search_reasoning_effort"] == "high"


# ─── assistants page: improve prompt flow ────────────────────────────────────

def test_assistants_improve_prompt_does_not_mutate_existing_widget(mock_env):
    """
    The 'Improve prompt' button must not write into the session-state key of an
    already-instantiated text_area widget. It bumps the prompt revision, stores
    the improved text under the NEW key, and reruns; the next render uses that
    new key.
    """
    from ui.pages.assistants import page_assistants
    import ui.pages.assistants as assistants_mod

    assistants_mod.st = mock_env
    mock_env.session_state.update({
        "show_assistant_form": True,
        "show_skill_form": True,
        "edit_assistant_id": None,
        "edit_skill_id": None,
    })
    mock_env._text_returns["assistant_prompt_text_0"] = "Original prompt"

    with patch("ui.pages.assistants.get_services",
               return_value={"TestSvc": SAMPLE_SERVICE}), \
         patch("ui.pages.assistants.improve_prompt_with_weak_model",
               return_value="Improved text") as mock_improve:
        mock_env.click("assistant_improve")
        invoke_page(page_assistants)

    assert mock_improve.called
    assert mock_env.session_state.get("assistant_prompt_revision") == 1
    assert mock_env.session_state.get("assistant_prompt_text_1") == "Improved text"
    assert not mock_env.errors

    text_area_keys = [
        kwargs.get("key")
        for name, _, kwargs in mock_env.calls
        if name == "text_area"
    ]
    assert "assistant_prompt_text_0" in text_area_keys
    assert "assistant_prompt_text" not in text_area_keys

    # Next render: the text_area must use the new revision key.
    mock_env.reset_clicks()
    mock_env._text_returns["assistant_prompt_text_1"] = "Improved text"
    mock_env.calls.clear()
    with patch("ui.pages.assistants.get_services",
               return_value={"TestSvc": SAMPLE_SERVICE}):
        invoke_page(page_assistants)

    text_area_keys = [
        kwargs.get("key")
        for name, _, kwargs in mock_env.calls
        if name == "text_area"
    ]
    assert "assistant_prompt_text_1" in text_area_keys


# ─── assistants page: new-assistant form order & defaults ────────────────────


def _open_assistant_create_form(mock_env):
    mock_env.session_state.update({
        "show_assistant_form": True,
        "show_skill_form": True,
        "edit_assistant_id": None,
        "edit_skill_id": None,
    })


def test_assistant_form_field_order(mock_env):
    """The create form renders basic fields before model/limits fields."""
    from ui.pages.assistants import page_assistants
    import ui.pages.assistants as assistants_mod

    assistants_mod.st = mock_env
    _open_assistant_create_form(mock_env)

    with patch("ui.pages.assistants.get_services",
               return_value={"TestSvc": SAMPLE_SERVICE}), \
         patch("ui.pages.assistants.service_supported_tools",
               return_value=["web_search"]):
        invoke_page(page_assistants)

    ordered_keys = [
        kwargs.get("key")
        for name, _, kwargs in mock_env.calls
        if kwargs.get("key")
    ]

    expected = [
        "assistant_name_input",
        "assistant_desc",
        "assistant_prompt_text_0",
        "assistant_service",
        "assistant_model",
        "assistant_temp",
        "assistant_tools_TestSvc",
        "assistant_max_calls",
        "assistant_max_tokens",
    ]
    positions = [ordered_keys.index(k) for k in expected]
    assert positions == sorted(positions), f"fields out of order: {expected}"


def test_assistant_form_max_tool_calls_default_three(mock_env):
    """New assistant form defaults max tool calls to 3."""
    from ui.pages.assistants import page_assistants
    import ui.pages.assistants as assistants_mod

    assistants_mod.st = mock_env
    _open_assistant_create_form(mock_env)

    with patch("ui.pages.assistants.get_services",
               return_value={"TestSvc": SAMPLE_SERVICE}):
        invoke_page(page_assistants)

    number_values = {
        kwargs.get("key"): kwargs.get("value")
        for name, _, kwargs in mock_env.calls
        if name == "number_input" and kwargs.get("key")
    }
    assert number_values.get("assistant_max_calls") == 3


def test_assistant_form_renders_reasoning_effort_select(mock_env):
    """Reasoning-effort select appears for services that support it."""
    from ui.pages.assistants import page_assistants
    import ui.pages.assistants as assistants_mod

    assistants_mod.st = mock_env
    _open_assistant_create_form(mock_env)

    re_svc = dict(SAMPLE_SERVICE)
    re_svc["extra_fields"] = [{
        "key": "reasoning_effort",
        "type": "select",
        "options": ["none", "low", "medium", "high", "max"],
        "default": "max",
    }]
    with patch("ui.pages.assistants.get_services",
               return_value={"TestSvc": re_svc}):
        invoke_page(page_assistants)

    select_keys = [
        kwargs.get("key")
        for name, _, kwargs in mock_env.calls
        if name == "selectbox" and kwargs.get("key")
    ]
    assert "assistant_reasoning_effort" in select_keys


def test_assistant_create_saves_reasoning_effort(mock_env):
    """Saving a new assistant persists reasoning_effort (default high)."""
    from ui.pages.assistants import page_assistants
    import ui.pages.assistants as assistants_mod

    assistants_mod.st = mock_env
    _open_assistant_create_form(mock_env)
    mock_env._text_returns["assistant_name_input"] = "Test RE"
    mock_env._text_returns["assistant_prompt_text_0"] = "You are helpful."

    re_svc = dict(SAMPLE_SERVICE)
    re_svc["extra_fields"] = [{
        "key": "reasoning_effort",
        "type": "select",
        "options": ["none", "low", "medium", "high", "max"],
        "default": "max",
    }]
    with patch("ui.pages.assistants.get_services",
               return_value={"TestSvc": re_svc}), \
         patch("ui.pages.assistants.create_assistant",
               return_value="newid") as mock_create:
        mock_env.click("assistant_save_btn")
        invoke_page(page_assistants)

    assert mock_create.called
    assert mock_create.call_args.kwargs["reasoning_effort"] == "high"
    assert mock_create.call_args.kwargs["max_tool_calls"] == 3

# ─── assistants/chat navigation: settings ↔ chat ─────────────────────────────

def test_assistants_page_back_to_chat_returns_to_chat(mock_env):
    """The '← Chat' button on the assistants page returns to chat with the
    currently edited/last active assistant preselected."""
    from ui.pages.assistants import page_assistants
    import ui.pages.assistants as assistants_mod

    assistants_mod.st = mock_env
    mock_env.session_state.update({
        "edit_assistant_id": "a1",
        "edit_skill_id": "a1",
        "last_active_entity_id": "a1",
        "selected_assistant_id": "a1",
        "selected_skill_id": "a1",
    })
    mock_env.click("assistant_back_chat")
    invoke_page(page_assistants)
    assert mock_env.session_state["current_page"] == "run"
    assert mock_env.session_state["selected_assistant_id"] == "a1"


# ─── orchestrator event rendering: tool_call/tool_result pairing ───────────

def test_strip_html_details_tags_removes_wrappers():
    """HTML <details>/<summary> tags must be stripped, inner text kept."""
    from ui.pages.orchestrator import _strip_html_details_tags

    text = (
        "<details><summary>Разбор текущего кода.</summary>"
        "Прочитаю целиком `dev_agent/agent_loop.py`."
        "</details> Продолжаю."
    )
    cleaned = _strip_html_details_tags(text)
    assert "<details" not in cleaned
    assert "</details" not in cleaned
    assert "<summary" not in cleaned
    assert "</summary" not in cleaned
    assert "Разбор текущего кода." in cleaned
    assert "Продолжаю." in cleaned


def test_strip_tool_calls_removes_details_tags():
    """The display cleaner must also remove details/summary wrappers."""
    from ui.pages.orchestrator import _strip_tool_calls

    text = (
        "Готово.\n\n"
        "<details><summary>Промежуточные шаги</summary>"
        "Читал файлы и проверял тесты."
        "</details>\n\n"
        "Все шаги выполнены."
    )
    cleaned = _strip_tool_calls(text)
    assert "<details" not in cleaned
    assert "<summary" not in cleaned
    assert "Все шаги выполнены." in cleaned


def test_render_tool_result_shows_call_and_first_two_lines(mock_env):
    """tool_result expands to a Call block + Result block with only the first
    two lines visible and a nested expander for the full payload."""
    from ui.pages import orchestrator as orch_mod

    orch_mod.st = mock_env
    with patch.object(orch_mod, "t", side_effect=lambda key, *a, **k: key):
        orch_mod._render_tool_result({
            "type": "tool_result",
            "tool": "read_file",
            "result": {
                "ok": True,
                "path": "ui/pages/history.py",
                "total_lines": 322,
                "content": "line one\nline two\nline three\nline four",
            },
        }, "English", call_ev={"type": "tool_call", "tool": "read_file",
                                  "args": {"path": "ui/pages/history.py"}})

    expander_headers = [
        args[0] if args else ""
        for name, args, _ in mock_env.calls
        if name == "expander"
    ]
    # Outer expander keeps the compact header.
    assert any("tool_result" in str(h) and "read_file" in str(h) for h in expander_headers)
    # Nested expander offers the full payload.
    assert any("event_tool_result_show_more" in str(h) for h in expander_headers)

    code_payloads = [
        args[0] if args else ""
        for name, args, _ in mock_env.calls
        if name == "code"
    ]
    # The first two lines of the result are visible immediately.
    assert any("line one\nline two" == str(p) for p in code_payloads)
    # The full result body is available in the nested expander.
    assert any("line three\nline four" in str(p) for p in code_payloads)


def test_render_tool_result_short_result_no_nested_expander(mock_env):
    """When the result fits in two lines, no nested expander is rendered."""
    from ui.pages import orchestrator as orch_mod

    orch_mod.st = mock_env
    with patch.object(orch_mod, "t", side_effect=lambda key, *a, **k: key):
        orch_mod._render_tool_result({
            "type": "tool_result",
            "tool": "verify_file",
            "result": {"ok": True, "path": "x.py"},
        }, "English", call_ev={"type": "tool_call", "tool": "verify_file",
                                  "args": {"path": "x.py"}})

    expander_headers = [
        args[0] if args else ""
        for name, args, _ in mock_env.calls
        if name == "expander"
    ]
    assert len(expander_headers) == 1
    assert any("tool_result" in str(h) for h in expander_headers)


def test_render_events_pairs_tool_call_with_tool_result(mock_env):
    """_render_events must pair each tool_call with its tool_result instead of
    rendering the call as a standalone line."""
    from ui.pages import orchestrator as orch_mod

    orch_mod.st = mock_env
    events = [
        {"type": "tool_call", "tool": "read_file",
         "args": {"path": "main.py"}},
        {"type": "tool_result", "tool": "read_file",
         "result": {"ok": True, "path": "main.py", "content": "a\nb"}},
    ]
    with patch.object(orch_mod, "t", side_effect=lambda key, *a, **k: key), \
         patch.object(orch_mod, "_render_tool_result", wraps=orch_mod._render_tool_result) as mock_render:
        orch_mod._render_events(events, "English")

    assert mock_render.call_count == 1
    # The tool_call must be passed as call_ev, not rendered separately.
    assert mock_render.call_args[1]["call_ev"] is events[0]
    # No standalone st.info for the tool call args.
    info_payloads = [
        args[0] if args else ""
        for name, args, _ in mock_env.calls
        if name == "info"
    ]
    assert not any("read_file" in str(p) for p in info_payloads)


def test_render_events_standalone_tool_call_falls_back(mock_env):
    """A tool_call without a following tool_result (interrupted loop) must
    still render standalone."""
    from ui.pages import orchestrator as orch_mod

    orch_mod.st = mock_env
    events = [
        {"type": "tool_call", "tool": "web_search", "args": {"query": "x"}},
    ]
    with patch.object(orch_mod, "t", side_effect=lambda key, *a, **k: key), \
         patch.object(orch_mod, "_render_event", wraps=orch_mod._render_event) as mock_event:
        orch_mod._render_events(events, "English")

    assert mock_event.call_count == 1


def test_chat_page_has_settings_and_new_dialog_buttons(mock_env):
    """The assistant chat page offers 'Settings' and 'New dialog' actions."""
    from ui.pages.chat import page_run_query
    import ui.pages.chat as chat_mod

    chat_mod.st = mock_env
    mock_env.session_state.update({
        "selected_assistant_id": "a1",
        "selected_skill_id": "a1",
        "active_thread_id": None,
        "attached_file_context": "",
        "attached_file_name": "",
        "force_send": False,
        "input_key": 0,
    })
    assistant_index = {
        "id": "a1", "name": "Test", "service": "Svc", "model": "m",
        "temperature": 0.5, "created_at": "2025-01-01T00:00:00",
        "tools": [], "max_tool_calls": 3,
    }
    assistant = {
        "id": "a1", "name": "Test", "service": "Svc", "model": "m",
        "temperature": 0.5, "text": "prompt", "tools": [],
        "max_tool_calls": 3, "max_tokens": 4096,
    }
    with patch.object(chat_mod, "load_assistants_index", return_value=[assistant_index]), \
         patch.object(chat_mod, "get_assistant_by_id", return_value=assistant), \
         patch.object(chat_mod, "list_assistant_files", return_value=[]), \
         patch.object(chat_mod, "load_assistant_files_context", return_value=""), \
         patch.object(chat_mod, "load_thread_messages", return_value=[]), \
         patch.object(chat_mod, "check_context",
                      return_value={"ok": True, "total_tokens": 10,
                                    "limit": 1000, "excess_chars": 0}), \
         patch.object(chat_mod, "get_file_uploader_types", return_value=[]):
        invoke_page(page_run_query)

    button_keys = [
        kwargs.get("key")
        for name, _, kwargs in mock_env.calls
        if name == "button"
    ]
    assert any(k and k.startswith("chat_settings_") for k in button_keys)
    assert any(k and k.startswith("chat_new_dialog_") for k in button_keys)


def test_chat_page_keeps_selected_assistant_across_reruns(mock_env):
    """The preselected assistant must survive reruns: page_run_query() must
    NOT remove selected_assistant_id/selected_skill_id from session_state,
    must NOT render the fallback assistant selector, and the settings/new
    dialog buttons must be bound to the actually selected assistant."""
    from ui.pages.chat import page_run_query
    import ui.pages.chat as chat_mod

    chat_mod.st = mock_env
    mock_env.session_state.update({
        "selected_assistant_id": "a2",
        "selected_skill_id": "a2",
        "active_thread_id": None,
        "attached_file_context": "",
        "attached_file_name": "",
        "force_send": False,
        "input_key": 0,
    })
    assistants_index = [
        {"id": "a1", "name": "Assistant One", "service": "Svc", "model": "m",
         "temperature": 0.5, "created_at": "2025-01-01T00:00:00",
         "tools": [], "max_tool_calls": 3},
        {"id": "a2", "name": "Assistant Two", "service": "Svc", "model": "m",
         "temperature": 0.5, "created_at": "2025-01-01T00:00:00",
         "tools": [], "max_tool_calls": 3},
    ]

    def get_assistant(aid):
        if aid not in ("a1", "a2"):
            return None
        return {"id": aid, "name": aid, "service": "Svc", "model": "m",
                "temperature": 0.5, "text": "prompt", "tools": [],
                "max_tool_calls": 3, "max_tokens": 4096}

    with patch.object(chat_mod, "load_assistants_index",
                      return_value=assistants_index), \
         patch.object(chat_mod, "get_assistant_by_id",
                      side_effect=get_assistant), \
         patch.object(chat_mod, "list_assistant_files", return_value=[]), \
         patch.object(chat_mod, "load_assistant_files_context", return_value=""), \
         patch.object(chat_mod, "load_thread_messages", return_value=[]), \
         patch.object(chat_mod, "check_context",
                      return_value={"ok": True, "total_tokens": 10,
                                    "limit": 1000, "excess_chars": 0}), \
         patch.object(chat_mod, "get_file_uploader_types", return_value=[]):
        # Two renders emulate two Streamlit reruns (e.g. file upload / button).
        invoke_page(page_run_query)
        invoke_page(page_run_query)

    # The selection is the user's active choice and must be kept.
    assert mock_env.session_state.get("selected_assistant_id") == "a2"
    assert mock_env.session_state.get("selected_skill_id") == "a2"

    # No fallback selector: the page stays on the preselected assistant.
    selectbox_keys = [
        kwargs.get("key")
        for name, _, kwargs in mock_env.calls
        if name == "selectbox"
    ]
    assert "chat_assistant_select" not in selectbox_keys

    # Settings / new-dialog buttons are bound to the selected assistant.
    button_keys = [
        kwargs.get("key")
        for name, _, kwargs in mock_env.calls
        if name == "button"
    ]
    assert any(k and k.startswith("chat_settings_") and k.endswith("_a2")
               for k in button_keys)
    assert any(k and k.startswith("chat_new_dialog_") and k.endswith("_a2")
               for k in button_keys)
