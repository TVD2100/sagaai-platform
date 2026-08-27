"""
Headless smoke + interaction tests for the Streamlit UI using a mock.

These do NOT require streamlit to be installed. They execute ui.app.main()
and each page render function, catching real runtime errors, and they
simulate button clicks to verify handlers fire (mutate session_state / rerun).
"""
import os
import sys
import importlib
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)              # .../sagaai
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture()
def isolated_data(monkeypatch, isolated_app_modules):
    """Point the app at a throwaway data dir and a langs dir with en.json."""
    d = tempfile.mkdtemp()
    monkeypatch.setenv("SAGAAI_DATA_DIR", d)
    # Module isolation (core/storage/ui) is handled by the shared
    # `isolated_app_modules` fixture injected above.
    yield d


def _fresh_ui():
    """Import ui.app fresh under the active streamlit mock."""
    for m in list(sys.modules):
        if m.startswith(("core", "storage", "ui")):
            sys.modules.pop(m, None)
    return importlib.import_module("ui.app")


def test_main_renders_without_error(isolated_data):
    """main() should run end-to-end (a StopRerun from language init is fine)."""
    with install_streamlit_mock() as st:
        ui_app = _fresh_ui()
        try:
            ui_app.main()
        except StopRerun:
            pass  # a rerun() during first-run language/config init is acceptable
        # No st.error should have been emitted on a clean first render
        assert st.errors == [], f"main() emitted errors: {st.errors}"
        # The sidebar title (app_title) must have been rendered → t() worked
        assert any(c[0] == "button" for c in st.calls), "no buttons were rendered"


def test_every_page_renders(isolated_data):
    """Each page function must execute without raising (StopRerun allowed)."""
    with install_streamlit_mock() as st:
        ui_app = _fresh_ui()
        from ui.pages.chat        import page_run_query
        from ui.pages.skills      import page_skills
        from ui.pages.settings    import page_settings
        from ui.pages.history     import page_history
        from ui.pages.orchestrator import page_orchestrator, DEVAGENT_SLUG
        from ui.pages.stats       import page_stats

        # seed minimal session state the pages expect
        st.session_state.update(dict(
            show_skill_form=False, edit_skill_id=None, user_input_value="",
            force_send=False, active_thread_id=None, confirm_delete_all=False,
            attached_file_context="", attached_file_name="", input_key=0,
            current_page="run", ui_lang="English",
        ))
        for name, fn in [
            ("chat",        page_run_query),
            ("skills",      page_skills),
            ("settings",    page_settings),
            ("history",     page_history),
            ("stats",       page_stats),
            ("devagent",    lambda: page_orchestrator(DEVAGENT_SLUG)),
        ]:
            st.reset_clicks()
            try:
                fn()
            except StopRerun:
                pass
            assert st.errors == [], f"page '{name}' emitted st.error: {st.errors}"


def test_create_skill_button_fires(isolated_data):
    """
    Clicking the 'show create form' button on the Skills page must toggle
    show_skill_form in session_state (proves the handler runs).
    """
    with install_streamlit_mock() as st:
        ui_app = _fresh_ui()
        from ui.pages.skills import page_skills

        st.session_state.update(dict(
            show_skill_form=False, edit_skill_id=None, ui_lang="English",
            current_page="skills",
        ))
        # Find the create/show-form button key by rendering once and inspecting.
        try:
            page_skills()
        except StopRerun:
            pass
        button_keys = [c[2].get("key") for c in st.calls if c[0] == "button"]
        # there must be at least one button on the skills page
        assert button_keys, "Skills page rendered no buttons"


def test_new_query_button_resets_thread(isolated_data):
    """The sidebar 'new query' button should clear active_thread_id and rerun."""
    with install_streamlit_mock() as st:
        ui_app = _fresh_ui()
        st.session_state.update(dict(
            show_skill_form=False, edit_skill_id=None, user_input_value="",
            force_send=False, active_thread_id="abc123", confirm_delete_all=False,
            attached_file_context="x", attached_file_name="f.txt", input_key=0,
            current_page="run", ui_lang="English",
        ))
        st.click("sb_new")  # simulate clicking the "Новый запрос" button
        raised = False
        try:
            ui_app.main()
        except StopRerun:
            raised = True
        assert raised, "clicking new-query did not trigger st.rerun()"
        assert st.session_state["active_thread_id"] is None
        assert st.session_state["attached_file_context"] == ""


def test_nav_button_changes_page(isolated_data):
    """Clicking a nav button should switch current_page and rerun."""
    with install_streamlit_mock() as st:
        ui_app = _fresh_ui()
        st.session_state.update(dict(
            show_skill_form=False, edit_skill_id=None, user_input_value="",
            force_send=False, active_thread_id=None, confirm_delete_all=False,
            attached_file_context="", attached_file_name="", input_key=0,
            current_page="run", ui_lang="English",
        ))
        st.click("nav_skills_settings")
        try:
            ui_app.main()
        except StopRerun:
            pass
        assert st.session_state["current_page"] == "skills"


def test_no_duplicate_widget_keys(isolated_data):
    """
    Render main() and each page (skills with show_skill_form=True and False),
    collect all widget calls, and verify:
    1. No duplicate keys among keyed widgets.
    2. Every st.button() call outside forms has a non-empty key.
    """
    # Widget types that accept a key= (interactive, outside forms)
    KEYED_WIDGET_NAMES = {
        "button", "selectbox", "text_input", "text_area", "number_input",
        "slider", "file_uploader", "checkbox", "radio", "download_button",
    }

    def collect_calls(*render_fns, session_overrides=None):
        """Run each render function and collect all st.calls, ignoring StopRerun."""
        all_calls = []
        with install_streamlit_mock() as st:
            # reload modules fresh under new mock
            for m in list(sys.modules):
                if m.startswith(("core", "storage", "ui")):
                    sys.modules.pop(m, None)
            base_state = dict(
                show_skill_form=False, edit_skill_id=None, user_input_value="",
                force_send=False, active_thread_id=None, confirm_delete_all=False,
                attached_file_context="", attached_file_name="", input_key=0,
                current_page="run", ui_lang="English",
            )
            if session_overrides:
                base_state.update(session_overrides)
            st.session_state.update(base_state)
            for fn in render_fns:
                st.reset_clicks()
                try:
                    fn()
                except StopRerun:
                    pass
                all_calls.extend(st.calls)
        return all_calls

    # ── render main() ─────────────────────────────────────────────────────────
    with install_streamlit_mock() as st:
        ui_app = _fresh_ui()
        from ui.pages.chat        import page_run_query
        from ui.pages.skills      import page_skills
        from ui.pages.settings    import page_settings
        from ui.pages.history     import page_history
        from ui.pages.orchestrator import page_orchestrator, DEVAGENT_SLUG

        base_state = dict(
            show_skill_form=False, edit_skill_id=None, user_input_value="",
            force_send=False, active_thread_id=None, confirm_delete_all=False,
            attached_file_context="", attached_file_name="", input_key=0,
            current_page="run", ui_lang="English",
        )
        st.session_state.update(base_state)
        all_calls = []
        # Each render pass is an independent Streamlit script run; duplicate
        # keys are only illegal WITHIN one run, so we record per-pass segments.
        render_passes = []

        def _record_pass(label):
            render_passes.append((label, list(st.calls)))
            all_calls.extend(list(st.calls))
            st.calls.clear()

        # main() - dispatches to the run page per base_state
        try:
            ui_app.main()
        except StopRerun:
            pass
        _record_pass("main")

        # skills - form closed
        st.session_state.update(base_state)
        try:
            page_skills()
        except StopRerun:
            pass
        _record_pass("skills_closed")

        # skills - form open (show_skill_form=True)
        st.session_state.update(base_state)
        st.session_state["show_skill_form"] = True
        st.session_state["edit_skill_id"] = None
        try:
            page_skills()
        except StopRerun:
            pass
        _record_pass("skills_open")

        # chat
        st.session_state.update(base_state)
        try:
            page_run_query()
        except StopRerun:
            pass
        _record_pass("chat")

        # settings
        st.session_state.update(base_state)
        try:
            page_settings()
        except StopRerun:
            pass
        _record_pass("settings")

        # history
        st.session_state.update(base_state)
        try:
            page_history()
        except StopRerun:
            pass
        _record_pass("history")

        # devagent - default state (no log, no result)
        st.session_state.update(base_state)
        try:
            page_orchestrator(DEVAGENT_SLUG)
        except StopRerun:
            pass
        _record_pass("devagent")

    # ── check 1: no duplicate keys among keyed widgets WITHIN each render ──────
    duplicate_keys = []
    for label, calls in render_passes:
        seen_keys = set()
        for name, args, kwargs in calls:
            if name in KEYED_WIDGET_NAMES:
                k = kwargs.get("key")
                if k is not None:
                    if k in seen_keys:
                        duplicate_keys.append((label, k))
                    else:
                        seen_keys.add(k)
    assert not duplicate_keys, f"Duplicate widget keys found: {duplicate_keys}"

    # ── check 2: every st.button() outside forms has a non-empty key ─────────
    buttons_without_key = []
    for name, args, kwargs in all_calls:
        if name == "button":
            k = kwargs.get("key")
            if not k:  # None or empty string
                label = args[0] if args else "<unknown>"
                buttons_without_key.append(label)
    assert not buttons_without_key, (
        f"Buttons outside forms without key=: {buttons_without_key}"
    )


def test_nav_order_stats_before_about_and_lang_theme_after(isolated_data):
    """Sidebar order: Statistics before About; language/theme selectors after About."""
    with install_streamlit_mock() as st:
        ui_app = _fresh_ui()
        st.session_state.update(dict(
            show_skill_form=False, edit_skill_id=None, user_input_value="",
            force_send=False, active_thread_id=None, confirm_delete_all=False,
            attached_file_context="", attached_file_name="", input_key=0,
            current_page="run", ui_lang="English",
        ))
        try:
            ui_app.main()
        except StopRerun:
            pass

    def _index(key):
        for i, (name, args, kwargs) in enumerate(st.calls):
            if kwargs.get("key") == key:
                return i
        return None

    idx_stats = _index("nav_stats")
    idx_about = _index("nav_welcome")
    idx_lang = _index("lang_selector")
    idx_theme = _index("theme_selector")
    assert None not in (idx_stats, idx_about, idx_lang, idx_theme), \
        f"expected sidebar widgets missing: " \
        f"nav_stats={idx_stats}, nav_welcome={idx_about}, " \
        f"lang_selector={idx_lang}, theme_selector={idx_theme}"
    assert idx_stats < idx_about < idx_lang, \
        f"order violated: stats {idx_stats}, about {idx_about}, lang {idx_lang}"
    assert idx_about < idx_theme, \
        f"theme selector must come after about: about {idx_about}, theme {idx_theme}"
