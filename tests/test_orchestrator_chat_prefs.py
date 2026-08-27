# -*- coding: utf-8 -*-
"""tests/test_orchestrator_chat_prefs.py - regression tests for:

1. The DevAgent-specific welcome message (orch_welcome_msg_devagent) on the
   orchestrator chat page, while custom employees keep orch_welcome_msg.
2. Persistence of the chat checkboxes (web search / economy mode / safety
   mode): values are saved into the orchestrator config and are used as the
   initial session-state defaults for every NEW dialog.
3. The single chat toolbar: each preference checkbox appears exactly once
   (no bottom-position duplicate).
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


def _markdown_calls(st):
    return [c[1][0] for c in st.calls if c[0] == "markdown"]


def _checkbox_by_key(st, key):
    for name, args, kwargs in st.calls:
        if name == "checkbox" and kwargs.get("key") == key:
            return kwargs
    return None


def _orch_dict(slug="custom1", config=None):
    """Build a minimal orchestrator dict; config is merged over the base."""
    cfg = {"strong_service": "DeepSeek", "strong_model": "m1"}
    if config:
        cfg.update(config)
    return {
        "slug": slug, "name": "Custom", "description": "",
        "is_builtin": False, "prompt_text": "", "config": cfg,
    }


def _render_chat(ui_env, monkeypatch, slug, orch_dict, history=()):
    """Render page_orchestrator(slug) with a mocked orchestrator record."""
    import ui.pages.orchestrator as orch_page
    monkeypatch.setattr(orch_page, "get_orchestrator",
                        lambda s: orch_dict if s == slug else None)
    monkeypatch.setattr(orch_page, "_assistant_has_api_key", lambda svc: True)
    st = ui_env
    st.session_state.update({"ui_lang": "English"})
    if history:
        st.session_state[f"orch_{slug}_history"] = list(history)
    _rerender(st, lambda: orch_page.page_orchestrator(slug))
    return orch_page


# ─── welcome message differentiation ──────────────────────────────────────────

def test_devagent_page_uses_devagent_welcome(ui_env, monkeypatch):
    """DevAgent must greet with orch_welcome_msg_devagent, not the generic text."""
    from core.i18n import t
    import ui.pages.orchestrator as orch_page

    slug = orch_page.DEVAGENT_SLUG
    _render_chat(ui_env, monkeypatch, slug, _orch_dict(slug=slug))

    expected = t("orch_welcome_msg_devagent", lang="English")
    generic = t("orch_welcome_msg", lang="English", name="DevAgent")
    md = _markdown_calls(ui_env)
    assert expected in md, f"devagent welcome missing in {md}"
    assert generic not in md, f"generic welcome leaked for DevAgent: {md}"


def test_custom_orchestrator_keeps_generic_welcome(ui_env, monkeypatch):
    """A custom employee must keep the name-parameterised generic welcome."""
    from core.i18n import t

    slug = "custom1"
    _render_chat(ui_env, monkeypatch, slug, _orch_dict(slug=slug))

    expected = t("orch_welcome_msg", lang="English", name="Custom")
    devagent_msg = t("orch_welcome_msg_devagent", lang="English")
    md = _markdown_calls(ui_env)
    assert expected in md, f"generic welcome missing in {md}"
    assert devagent_msg not in md, f"devagent welcome leaked for custom: {md}"


# ─── checkbox persistence ─────────────────────────────────────────────────────

def test_checkbox_values_come_from_saved_config(ui_env, monkeypatch):
    """Saved prefs must drive the checkbox widgets on a fresh render."""
    slug = "custom1"
    orch = _orch_dict(slug=slug, config={
        "chat_web_search": True,
        "chat_economy_mode": False,
        "chat_safety_mode": False,
    })
    _render_chat(ui_env, monkeypatch, slug, orch)

    ws = _checkbox_by_key(ui_env, f"orch_{slug}_web_search")
    eco = _checkbox_by_key(ui_env, f"orch_{slug}_economy_mode")
    safety = _checkbox_by_key(ui_env, f"orch_{slug}_safety_mode")
    assert ws is not None and callable(ws.get("on_change")), f"web_search: {ws}"
    assert eco is not None and callable(eco.get("on_change")), f"economy: {eco}"
    assert safety is not None and callable(safety.get("on_change")), f"safety: {safety}"
    assert ws["value"] is True, f"web_search value: {ws}"
    assert eco["value"] is False, f"economy value: {eco}"
    assert safety["value"] is False, f"safety value: {safety}"

    # The session defaults must match as well (new session seeds from config).
    assert ui_env.session_state[f"orch_{slug}_web_search"] is True
    assert ui_env.session_state[f"orch_{slug}_economy_mode"] is False
    assert ui_env.session_state[f"orch_{slug}_safety_mode"] is False

    # The toolbar is rendered once: no bottom-duplicate widgets may exist.
    for pref in ("web_search", "economy_mode", "safety_mode"):
        assert _checkbox_by_key(ui_env, f"orch_{pref}_bottom_{slug}") is None


def test_checkbox_defaults_without_saved_config(ui_env, monkeypatch):
    """Without saved prefs the defaults are: web off, economy on, safety on."""
    slug = "custom1"
    _render_chat(ui_env, monkeypatch, slug, _orch_dict(slug=slug))

    ws = _checkbox_by_key(ui_env, f"orch_{slug}_web_search")
    eco = _checkbox_by_key(ui_env, f"orch_{slug}_economy_mode")
    safety = _checkbox_by_key(ui_env, f"orch_{slug}_safety_mode")
    assert ws is not None and ws["value"] is False
    assert eco is not None and eco["value"] is True
    assert safety is not None and safety["value"] is True


def test_toolbar_renders_once_without_bottom_duplicates(ui_env, monkeypatch):
    """The toolbar renders exactly once: canonical checkbox keys exist and
    no bottom-position duplicates are created."""
    slug = "custom1"
    _render_chat(ui_env, monkeypatch, slug, _orch_dict(slug=slug))

    for pref in ("web_search", "economy_mode", "safety_mode"):
        widget = _checkbox_by_key(ui_env, f"orch_{slug}_{pref}")
        assert widget is not None, f"{pref} checkbox missing"
        assert widget["key"] == f"orch_{slug}_{pref}"
        assert _checkbox_by_key(ui_env, f"orch_{pref}_top_{slug}") is None
        assert _checkbox_by_key(ui_env, f"orch_{pref}_bottom_{slug}") is None


def test_save_chat_pref_merges_config(ui_env, monkeypatch):
    """_save_chat_pref must persist the new value into the orch config,
    preserving unrelated keys."""
    import ui.pages.orchestrator as orch_page

    orch = {"slug": "custom1", "name": "Custom", "config": {
        "strong_service": "DeepSeek", "strong_model": "m1",
        "chat_web_search": False,
    }}
    monkeypatch.setattr(orch_page, "get_orchestrator",
                        lambda s: orch if s == "custom1" else None)
    saved = {}
    monkeypatch.setattr(orch_page, "save_orchestrator",
                        lambda slug, config=None, **kw: (
                            saved.update({"slug": slug,
                                          "config": dict(config or {})}),
                            True)[-1])

    ok = orch_page._save_chat_pref("custom1", "web_search", True)

    assert ok is True
    assert saved["slug"] == "custom1"
    assert saved["config"]["chat_web_search"] is True
    assert saved["config"]["strong_service"] == "DeepSeek"  # preserved
    assert saved["config"]["strong_model"] == "m1"           # preserved


def test_save_chat_pref_rejects_unknown_key_and_missing_orch(ui_env, monkeypatch):
    """Unknown pref keys and missing orchestrators must not write anything."""
    import ui.pages.orchestrator as orch_page

    monkeypatch.setattr(orch_page, "get_orchestrator", lambda s: None)
    called = []
    monkeypatch.setattr(orch_page, "save_orchestrator",
                        lambda *a, **k: called.append(1))

    assert orch_page._save_chat_pref("custom1", "nope", True) is False
    assert orch_page._save_chat_pref("custom1", "web_search", True) is False
    assert called == []


def test_chat_prefs_fallback_on_non_dict_config(ui_env, monkeypatch):
    """A broken config payload must not crash _chat_prefs / state init."""
    import ui.pages.orchestrator as orch_page

    orch = _orch_dict(slug="custom1")
    orch["config"] = None
    _render_chat(ui_env, monkeypatch, "custom1", orch)

    assert ui_env.session_state["orch_custom1_web_search"] is False
    assert ui_env.session_state["orch_custom1_economy_mode"] is True
    assert ui_env.session_state["orch_custom1_safety_mode"] is True
