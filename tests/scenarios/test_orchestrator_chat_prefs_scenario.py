# -*- coding: utf-8 -*-
"""tests/scenarios/test_orchestrator_chat_prefs_scenario.py - user-level
scenario tests for the orchestrator chat-page usability fixes.

Scenarios (given -> when -> then), walking the public UI entry point
``ui.pages.orchestrator.page_orchestrator``:

  Scenario 1 - a fresh DevAgent dialog greets with the DevAgent-specific
               welcome and shows the default chat-preference checkboxes
               (web off, economy on, safety on).
  Scenario 2 - saved chat preferences survive a NEW dialog: the checkboxes
               are initialised from the persisted orchestrator config.
  Scenario 3 - toggling a preference checkbox persists it into the
               orchestrator config and survives the next render.
  Scenario 4 - the toolbar is rendered once: toggling a checkbox persists
               the value and never creates bottom-duplicate widgets.
  Scenario 5 - disabling the Safety checkbox persists chat_safety_mode=False
               into the config and survives a brand-new render.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture()
def page_env(monkeypatch, tmp_path):
    """Isolated DATA_DIR + fresh ui.* modules under the streamlit mock."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    for m in list(sys.modules):
        if m == "ui" or m.startswith("ui."):
            sys.modules.pop(m, None)
    with install_streamlit_mock() as st:
        yield st, monkeypatch


def _render(st, monkeypatch, slug, config=None):
    """Render page_orchestrator(slug) with a mocked orchestrator record.

    Returns the freshly imported page module.
    """
    import ui.pages.orchestrator as orch_page

    cfg = {"strong_service": "DeepSeek", "strong_model": "m1"}
    if config:
        cfg.update(config)
    orch = {
        "slug": slug, "name": "DevAgent" if slug == orch_page.DEVAGENT_SLUG else "Employee",
        "description": "", "is_builtin": slug == orch_page.DEVAGENT_SLUG,
        "prompt_text": "", "config": cfg,
    }
    monkeypatch.setattr(orch_page, "get_orchestrator",
                        lambda s: orch if s == slug else None)
    monkeypatch.setattr(orch_page, "_assistant_has_api_key", lambda svc: True)
    # With no user text the chat_input must return None: the mock's generic
    # stub object is truthy, so an unwrapped chat_input would push the page
    # into the real send path, start an agent loop and hide the bottom
    # toolbar (agent_is_active) on subsequent renders.
    def _no_input(*a, **k):
        st._rec("chat_input", a, k)
        return None
    monkeypatch.setattr(st, "chat_input", _no_input)
    st.session_state.update({"ui_lang": "English"})
    try:
        orch_page.page_orchestrator(slug)
    except StopRerun:
        pass
    return orch_page


def _markdown_calls(st):
    return [c[1][0] for c in st.calls if c[0] == "markdown"]


def _checkbox(st, key):
    for name, args, kwargs in st.calls:
        if name == "checkbox" and kwargs.get("key") == key:
            return kwargs
    return None


def test_scenario_devagent_welcome_and_defaults(page_env):
    """Scenario 1: opening a fresh DevAgent dialog.

    Given a user with no saved preferences and no history,
    when  they open the DevAgent chat page,
    then  the DevAgent-specific welcome is shown (not the generic one) and
          the preference checkboxes appear with the historical defaults:
          web search off, economy on, safety on.
    """
    from core.i18n import t

    st, monkeypatch = page_env
    orch_page = _render(st, monkeypatch, "dev_agent")
    slug = orch_page.DEVAGENT_SLUG

    expected = t("orch_welcome_msg_devagent", lang="English")
    generic = t("orch_welcome_msg", lang="English", name="DevAgent")
    md = _markdown_calls(st)
    assert expected in md, f"DevAgent welcome missing: {md}"
    assert generic not in md, f"generic welcome leaked: {md}"

    ws = _checkbox(st, f"orch_{slug}_web_search")
    eco = _checkbox(st, f"orch_{slug}_economy_mode")
    safety = _checkbox(st, f"orch_{slug}_safety_mode")
    assert ws is not None and ws["value"] is False, f"web-search: {ws}"
    assert eco is not None and eco["value"] is True, f"economy: {eco}"
    assert safety is not None and safety["value"] is True, f"safety: {safety}"


def test_scenario_saved_prefs_restored_in_new_dialog(page_env):
    """Scenario 2: saved preferences survive a new dialog.

    Given the orchestrator config holds chat_web_search=True,
          chat_economy_mode=False and chat_safety_mode=False from a
          previous session,
    when  the user opens a NEW dialog for the same employee,
    then  the checkboxes are initialised from the persisted values and the
          session state matches them.
    """
    st, monkeypatch = page_env
    slug = "custom1"
    _render(st, monkeypatch, slug, config={
        "chat_web_search": True,
        "chat_economy_mode": False,
        "chat_safety_mode": False,
    })

    ws = _checkbox(st, f"orch_{slug}_web_search")
    eco = _checkbox(st, f"orch_{slug}_economy_mode")
    safety = _checkbox(st, f"orch_{slug}_safety_mode")
    assert ws is not None and ws["value"] is True, f"web-search: {ws}"
    assert eco is not None and eco["value"] is False, f"economy: {eco}"
    assert safety is not None and safety["value"] is False, f"safety: {safety}"

    assert st.session_state[f"orch_{slug}_web_search"] is True
    assert st.session_state[f"orch_{slug}_economy_mode"] is False
    assert st.session_state[f"orch_{slug}_safety_mode"] is False


def test_scenario_toggling_checkbox_persists(page_env):
    """Scenario 3: toggling a preference persists it and survives renders.

    Given a fresh DevAgent dialog with the default preferences,
    when  the user switches the web-search checkbox ON,
    then  the new value is saved into the orchestrator config (merging with
          the existing keys) and the next render shows the checkbox ON.
    """
    st, monkeypatch = page_env
    slash = "dev_agent"
    orch_page = _render(st, monkeypatch, slash)
    slug = orch_page.DEVAGENT_SLUG
    top_key = f"orch_{slug}_web_search"

    saved_configs = []
    monkeypatch.setattr(
        orch_page, "save_orchestrator",
        lambda slug_, config=None, **kw: (
            saved_configs.append(dict(config or {})) or True))

    widget = _checkbox(st, top_key)
    assert widget is not None, "web-search checkbox missing"
    on_change = widget.get("on_change")
    assert callable(on_change), f"web-search on_change: {widget}"

    # Real Streamlit updates the stateful slot BEFORE firing on_change.
    st.session_state[top_key] = True
    on_change(slug, "web_search")

    assert st.session_state[f"orch_{slug}_web_search"] is True
    assert any(cfg.get("chat_web_search") is True for cfg in saved_configs), \
        f"web-search pref not persisted: {saved_configs}"
    assert saved_configs[0]["strong_service"] == "DeepSeek"  # merged, not reset

    # There must be no bottom-duplicate widget: the toolbar is rendered once.
    assert st.session_state.get(f"orch_web_search_bottom_{slug}") is None

    # The next render (no simulated click) must reflect the persisted value.
    st.calls.clear()
    try:
        orch_page.page_orchestrator(slug)
    except StopRerun:
        pass

    ws = _checkbox(st, top_key)
    assert ws is not None and ws["value"] is True, f"web-search: {ws}"
    assert _checkbox(st, f"orch_web_search_bottom_{slug}") is None


def test_scenario_single_toolbar_after_toggle(page_env):
    """Scenario 4: the toolbar is rendered once and a toggle persists.

    Given a fresh dialog with the single toolbar rendered,
    when  the user switches the web-search checkbox ON,
    then  the canonical session value changes, the preference is persisted
          into the config, and no bottom-duplicate widget exists.
    """
    st, monkeypatch = page_env
    orch_page = _render(st, monkeypatch, "dev_agent")
    slug = orch_page.DEVAGENT_SLUG
    top_key = f"orch_{slug}_web_search"

    saved_configs = []
    monkeypatch.setattr(
        orch_page, "save_orchestrator",
        lambda slug_, config=None, **kw: (
            saved_configs.append(dict(config or {})) or True))

    widget = _checkbox(st, top_key)
    assert widget is not None, "web-search checkbox missing"
    on_change = widget.get("on_change")
    assert callable(on_change), f"web-search on_change: {widget}"

    # Real Streamlit updates the stateful slot BEFORE firing on_change.
    st.session_state[top_key] = True
    on_change(slug, "web_search")

    assert st.session_state[f"orch_{slug}_web_search"] is True
    assert _checkbox(st, f"orch_{slug}_web_search") is not None
    assert _checkbox(st, f"orch_web_search_bottom_{slug}") is None
    assert _checkbox(st, f"orch_web_search_top_{slug}") is None
    assert any(cfg.get("chat_web_search") is True for cfg in saved_configs)


def test_scenario_safety_mode_off_persists(page_env):
    """Scenario 5: disabling Safety persists chat_safety_mode=False.

    Given a fresh DevAgent dialog (safety default ON),
    when  the user unchecks the Safety checkbox,
    then  the new value is saved into the orchestrator config and a fresh
          render (new session state) still shows Safety OFF.
    """
    st, monkeypatch = page_env
    orch_page = _render(st, monkeypatch, "dev_agent")
    slug = orch_page.DEVAGENT_SLUG
    safety_key = f"orch_{slug}_safety_mode"

    saved_configs = []
    monkeypatch.setattr(
        orch_page, "save_orchestrator",
        lambda slug_, config=None, **kw: (
            saved_configs.append(dict(config or {})) or True))

    widget = _checkbox(st, safety_key)
    assert widget is not None, "safety checkbox missing"
    on_change = widget.get("on_change")
    assert callable(on_change), f"safety on_change: {widget}"
    assert widget["value"] is True  # default

    st.session_state[safety_key] = False
    on_change(slug, "safety_mode")

    assert st.session_state[safety_key] is False
    assert any(cfg.get("chat_safety_mode") is False for cfg in saved_configs), \
        f"safety pref not persisted: {saved_configs}"

    # Simulate a brand-new session: wipe the page's session slots but keep the
    # persisted config (the injected get_orchestrator now returns the saved
    # value through saved_configs).
    monkeypatch.setattr(
        orch_page, "get_orchestrator",
        lambda s: {
            "slug": slug, "name": "DevAgent", "description": "",
            "is_builtin": True, "prompt_text": "",
            "config": {"strong_service": "DeepSeek", "strong_model": "m1",
                       "chat_safety_mode": False},
        } if s == slug else None)
    for key in [safety_key, f"orch_{slug}_history", f"orch_{slug}_loop_state"]:
        st.session_state.pop(key, None)
    st.calls.clear()
    try:
        orch_page.page_orchestrator(slug)
    except StopRerun:
        pass

    re_rendered = _checkbox(st, safety_key)
    assert re_rendered is not None and re_rendered["value"] is False, \
        f"safety reset on fresh render: {re_rendered}"
    assert st.session_state[safety_key] is False
