# -*- coding: utf-8 -*-
"""ui.app_restore - theme switching, UI state restore and deep links.

These helpers only touch Streamlit session state and the browser URL; they
hold no navigation logic of their own. They live outside ui.app so the
application module stays readable, and ui.app re-exports every name below.
"""
import json

import streamlit as st

_THEME_MODES = ["System", "Light", "Dark"]


def _apply_theme(mode: str, restore_payload: str = "") -> None:
    """Persist the selected UI theme without losing the current page.

    Streamlit reads the active theme from localStorage only when the parent
    page loads, so a full browser reload is unavoidable. The JS must run in
    the top-level document: st.components.v1.html renders inside a sandboxed
    iframe whose sandbox flags do not include allow-top-navigation, so
    location.replace would be silently ignored. st.html with
    unsafe_allow_javascript=True executes the same script in the main
    document instead. The reload normally wipes the server-side session
    state and falls back to the welcome page. To keep the user where they
    were, a minimal state snapshot is attached to a URL query parameter
    before the reload; ui.app.main() restores it on the first rerun after
    the reload (see _restore_ui_reload_state).
    """
    script = (
        "(function () {"
        "  var w = window.parent === window ? window : window.parent;"
        "  var path = '/';"
        "  try { path = w.location.pathname || '/'; } catch (e) {}"
        "  var key = 'stActiveTheme-' + path + '-v2';"
        "  w.localStorage.setItem(key, JSON.stringify({mode}));"
        "  var url = new URL(w.location.href);"
        "  url.searchParams.set('_sagaai_ui_restore', {restore});"
        "  w.location.replace(url.toString());"
        "})();"
    ).replace("{mode}", json.dumps(mode))
    script = script.replace("{restore}", json.dumps(restore_payload))
    st.html(
        '<!doctype html><html><body><script>{script}</script></body></html>'.replace("{script}", script),
        unsafe_allow_javascript=True,
    )




def _sync_saved_theme(mode: str, restore_payload: str = "") -> None:
    """One-time sync of localStorage with the saved theme on initial page load.

    Streamlit paints the interface from localStorage whenever the parent page
    loads; the sidebar selectbox value only mirrors the server-side config.
    Manual switches persist the theme via _apply_theme, so after those the
    stored value usually matches. On a cold start (server restart, a new
    browser session, another port/origin) the stored value may be missing or
    stale while the config still says 'Dark': the sidebar then shows 'Dark'
    but the interface is painted light, and it only fixes itself when the
    user re-selects the theme by hand. This script compares both sides and,
    only when they differ, writes the saved value and does the single
    browser reload Streamlit needs; the restore query param carries the
    page/dialog snapshot so the user stays where they were (see _apply_theme
    and _restore_ui_reload_state).
    """
    script = (
        "(function () {"
        "  var w = window.parent === window ? window : window.parent;"
        "  var path = '/';"
        "  try { path = w.location.pathname || '/'; } catch (e) {}"
        "  var key = 'stActiveTheme-' + path + '-v2';"
        "  var stored = null;"
        "  try { stored = w.localStorage.getItem(key); } catch (e) { return; }"
        "  if (stored === JSON.stringify({mode})) { return; }"
        "  try { w.localStorage.setItem(key, JSON.stringify({mode})); } catch (e) {}"
        "  if (stored === null && JSON.stringify({mode}) === JSON.stringify('System')) { return; }"
        "  var url = new URL(w.location.href);"
        "  url.searchParams.set('_sagaai_ui_restore', {restore});"
        "  w.location.replace(url.toString());"
        "})();"
    ).replace("{mode}", json.dumps(mode))
    script = script.replace("{restore}", json.dumps(restore_payload))
    st.html(
        '<!doctype html><html><body><script>{script}</script></body></html>'.replace("{script}", script),
        unsafe_allow_javascript=True,
    )


def _sync_saved_theme_once(cfg: dict) -> None:
    """Run the cold-start localStorage sync of the saved theme exactly once.

    The flag survives the browser reload the sync triggers (the reload lands
    in the same server session), so the page is not reloaded in a loop.
    """
    if st.session_state.get('_ui_theme_synced'):
        return
    st.session_state['_ui_theme_synced'] = True
    saved_theme = cfg.get('ui_theme', '')
    if saved_theme in _THEME_MODES:
        _sync_saved_theme(saved_theme, _build_ui_restore_payload())


def _build_ui_restore_payload() -> str:
    """Return a JSON snapshot of the state needed to restore the UI after a reload.

    Kept intentionally small: the current page, the active assistant/orchestrator
    dialog, and the entity that drives the context-dependent sidebar actions.
    Dialog message history itself is persisted in the database and reloaded by
    the target page, so it is not serialised into the URL.
    """
    data = {}
    page = st.session_state.get("current_page")
    if page:
        data["page"] = str(page)
    tid = st.session_state.get("active_thread_id")
    if tid:
        data["active_thread_id"] = str(tid)
    assistant_id = (
        st.session_state.get("selected_assistant_id")
        or st.session_state.get("selected_skill_id")
    )
    if assistant_id:
        data["selected_assistant_id"] = str(assistant_id)
    entity_type = st.session_state.get("last_active_entity_type")
    if entity_type:
        data["last_active_entity_type"] = str(entity_type)
    entity_id = st.session_state.get("last_active_entity_id")
    if entity_id:
        data["last_active_entity_id"] = str(entity_id)
    if page and page.startswith("orchestrator:") and not page.startswith("orchestrator_settings:"):
        slug = page.split(":", 1)[1]
        orch_tid = st.session_state.get(f"orch_{slug}_thread_id")
        if orch_tid:
            data["orch_thread_id"] = str(orch_tid)
    return json.dumps(data)


def _restore_ui_reload_state() -> None:
    """Restore the page/dialog snapshot after a theme-switch reload.

    Runs once per browser session. If the URL contains the restore marker it
    is applied to session_state and then removed from the URL, so a later
    manual reload does not resurrect the previous dialog state.
    """
    if st.session_state.get("_theme_restore_handled"):
        return
    try:
        raw = st.query_params.get("_sagaai_ui_restore")
    except Exception:
        raw = None
    if not raw:
        return
    st.session_state["_theme_restore_handled"] = True
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    page = data.get("page")
    if page:
        st.session_state["current_page"] = str(page)
    if data.get("active_thread_id"):
        st.session_state["active_thread_id"] = str(data["active_thread_id"])
    if data.get("selected_assistant_id"):
        st.session_state["selected_assistant_id"] = str(data["selected_assistant_id"])
        st.session_state["selected_skill_id"] = str(data["selected_assistant_id"])
    if data.get("last_active_entity_type"):
        st.session_state["last_active_entity_type"] = str(data["last_active_entity_type"])
    if data.get("last_active_entity_id"):
        st.session_state["last_active_entity_id"] = str(data["last_active_entity_id"])
    if page and page.startswith("orchestrator:") and not page.startswith("orchestrator_settings:"):
        slug = page.split(":", 1)[1]
        import ui.pages.orchestrator as _orch_page
        _orch_page._init_orch_state(slug)
        orch_tid = data.get("orch_thread_id")
        if orch_tid:
            try:
                _orch_page._load_thread(slug, str(orch_tid))
            except Exception:
                pass
    try:
        if "_sagaai_ui_restore" in st.query_params:
            st.query_params.pop("_sagaai_ui_restore", None)
    except Exception:
        pass


def _handle_thread_deeplink() -> None:
    """Navigate to an employee thread passed as URL query parameters.

    The Copy-URL toolbar buttons build a deep link of the form
    ``?orchestrator=<slug>&thread=<tid>`` (``thread`` is optional).  On the
    first run with such a link the target dialog is loaded and the
    parameters are removed from the URL, so a later manual reload starts
    on the welcome page instead of resurrecting the same deep link.
    """
    if st.session_state.get("_thread_deeplink_handled"):
        return
    try:
        slug = (st.query_params.get("orchestrator") or "").strip()
        tid = (st.query_params.get("thread") or "").strip()
    except Exception:
        return
    if not slug:
        return
    st.session_state["_thread_deeplink_handled"] = True
    import ui.pages.orchestrator as _orch_page
    _orch_page._init_orch_state(slug)
    if tid:
        try:
            _orch_page._load_thread(slug, tid)
        except Exception:
            # Unknown / corrupt thread id: start a fresh dialog instead.
            _orch_page._reset_dialog(slug)
    else:
        _orch_page._reset_dialog(slug)
    st.session_state["last_active_entity_type"] = "orchestrator"
    st.session_state["last_active_entity_id"] = slug
    st.session_state["current_page"] = f"orchestrator:{slug}"
    try:
        st.query_params.pop("orchestrator", None)
        st.query_params.pop("thread", None)
    except Exception:
        pass
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
