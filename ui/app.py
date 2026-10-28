# -*- coding: utf-8 -*-
"""
ui.app - main Streamlit application: sidebar navigation and page dispatch.
Requires Streamlit.

Navigation structure:
  - Employees (orchestrators): click → chat immediately.
  - Assistants: click → chat immediately.
    A fixed block of up to 5 assistants is always visible (sorted by the
    latest dialogue time, or by creation time for assistants without
    dialogues - newest first). The rest stays in a collapsed expander with
    a search field.
  - "New dialog" button: context-dependent, starts fresh chat with last selected entity.
  - History button right after the active dialog indicator.
  - Settings: Employees, Assistants, Skills, LLM Providers, Language.
  - About / Welcome at the bottom.
"""
import json

import streamlit as st

from core.i18n import t, get_langs
from core.config import load_config, save_config, get_default_ui_lang
from core.assistants import load_assistants_index
from core.threads import load_thread_meta, list_chat_threads
from core.dependency_check import (
    check_dependencies,
    missing_packages,
    outdated_lines,
)
from core.bootstrap import ensure_instructions, ensure_devagent_settings
import core.default_imports as default_imports
from core.env_loader import load_env_from_shell_profiles
from core.auth import require_auth
from core.recent_assistants import record_assistant_use
from core.assistant_nav import sort_assistants, split_nav_lists, DEFAULT_VISIBLE_ASSISTANTS
from core.orchestrator_nav import sort_orchestrators, DEFAULT_VISIBLE_ORCHESTRATORS
from core.threads_devagent import list_devagent_threads, migrate_platform_workspace_meta

from ui.pages.welcome  import page_welcome
from ui.pages.chat     import page_run_query
from ui.pages.skills   import page_skills
from ui.pages.settings import page_settings
from ui.pages.history  import page_history
from ui.components.flash import render_flash

from core.orchestrators import list_orchestrators, DEVAGENT_SLUG
from core.version import __version__ as PLATFORM_VERSION
from ui.pages.orchestrator import page_orchestrator
from ui.pages.orchestrator_settings import page_orchestrator_settings
from ui.pages.orchestrators import page_orchestrators
from ui.pages.skills_library import page_skills_library
from ui.pages.marketplace import page_marketplace
from core.marketplace import is_enabled as marketplace_enabled
from ui.pages.storage import page_storage
from ui.pages.connectors import page_connectors
from ui.pages.stats import page_stats
from ui.pages.updates import page_updates
from ui.pages.access import page_access


# ── Icons ─────────────────────────────────────────────────────────────────────
_ORCH_ICON = "\U0001f916"          # custom orchestrators: robot
_DEVAGENT_ICON = "\U0001f6e0\ufe0f"      # built-in DevAgent: wrench + hammer
_ASSISTANT_ICON = "\U0001f9e9"      # assistants
_SLIB_ICON = "\U0001f9e0"          # skills library: puzzle piece
_MARKETPLACE_ICON = "\U0001f6d2"    # marketplace: shopping trolley
_THEME_MODES = ["System", "Light", "Dark"]


def _build_orch_nav():
    """Return (visible, collapsed) employee navigation entries.

    Each entry is a tuple (page_id, label, slug).

    The first DEFAULT_VISIBLE_ORCHESTRATORS (5) entries are always shown as
    a plain block; the rest stay inside the collapsed "All" expander next
    to a search field. The global order is stable across app restarts and
    follows the employees' activity: the newest dialogue time when the
    orchestrator has dialogues (threads from the DevAgent database), and
    the orchestrator creation time otherwise - a freshly created employee
    lands at the very top.
    """
    try:
        orch_list = list_orchestrators()
        devagent_threads = list_devagent_threads()
    except Exception:
        # Keep the rest of the navigation usable if the DB is stale.
        return [], []

    entries = []
    for orch in sort_orchestrators(orch_list, devagent_threads):
        slug = orch.get("slug", "")
        if not slug:
            continue
        name = orch.get("name", slug)
        icon = _DEVAGENT_ICON if slug == DEVAGENT_SLUG else _ORCH_ICON
        entries.append((f"orchestrator:{slug}", f"{icon} {name}", slug))
    return split_nav_lists(entries, DEFAULT_VISIBLE_ORCHESTRATORS)


def _build_assistants_nav(lang: str):
    """Return (visible_assistants, collapsed_assistants) for sidebar navigation.

    Each is a list of (assistant_id, name) tuples.

    The first DEFAULT_VISIBLE_ASSISTANTS (5) entries are always shown as a
    plain block; the rest stay inside the collapsed "All" expander.

    The global order is stable across app restarts and follows the
    assistants' activity: the newest chat-thread update when the assistant
    has dialogues, otherwise the assistant creation time. A freshly created
    assistant therefore appears right at the top. The session-only
    "recent_assistant_ids" list is intentionally NOT used for ordering.
    """
    try:
        all_assistants = load_assistants_index()
        chat_threads = list_chat_threads()
    except Exception:
        # If the DB schema is stale or any other error occurs during load,
        # return empty lists so the sidebar still renders the rest of the
        # navigation (employees, settings, etc.).
        return [], []
    assistant_map = {s["id"]: s["name"] for s in all_assistants if s.get("id") and s.get("name")}

    ordered = sort_assistants(all_assistants, chat_threads)
    entries = [(s["id"], s["name"]) for s in ordered if s.get("id") in assistant_map]
    visible, collapsed = split_nav_lists(entries, DEFAULT_VISIBLE_ASSISTANTS)
    return visible, collapsed


#: Marketplace subsections: (state key, entity type, agent kind, icon, label key).
_MARKETPLACE_SECTIONS = (
    ("agents", "agent", "orchestrator", _ORCH_ICON, "nav_settings_employees"),
    ("helpers", "agent", "assistant", _ASSISTANT_ICON, "nav_settings_assistants"),
    ("skills", "skill", "", _SLIB_ICON, "nav_settings_skills"),
    ("rag", "rag", "", "\U0001f5c4\ufe0f", "nav_storage"),
    ("mcp", "mcp", "", "\U0001f517", "nav_connectors"),
)

#: Settings entries that stay visible without expanding the block.
_SETTINGS_PRIMARY = (
    ("settings", "\U0001f50c", "nav_settings_providers"),
    ("connectors", "\U0001f517", "nav_connectors"),
    ("updates", "\U0001f504", "nav_updates"),
)

#: Settings entries kept inside the collapsed "More" block.
_SETTINGS_MORE = (
    ("orchestrators", "\U0001f916", "nav_settings_employees"),
    ("skills", "\U0001f9e9", "nav_settings_assistants"),
    ("skills_library", "\U0001f9e0", "nav_settings_skills"),
    ("storage", "\U0001f5c4\ufe0f", "nav_storage"),
    ("access", "\U0001f510", "nav_access"),
    ("stats", "\U0001f4ca", "nav_stats"),
    ("welcome", "\U0001f3e0", "nav_about"),
)


def active_marketplace_section() -> str:
    """Return the marketplace subsection selected in the sidebar.

    The showcase reads this value on render: the sidebar entry and the page
    tabs share one state key, so a click in either place lands on the same
    filter.
    """
    return str(st.session_state.get("mp_section") or "agents")


def _render_nav_button(
    page_id: str, icon: str, label_key: str, page: str, lang: str
) -> None:
    """Render one sidebar entry and switch to its page when pressed."""
    active = (page == page_id) or (
        page_id == "orchestrators" and page.startswith("orchestrator_settings:")
    )
    if active:
        st.markdown('<div class="nav-active">', unsafe_allow_html=True)
    if st.button(
        f"{icon} {t(label_key, lang=lang)}",
        key=f"nav_{page_id}",
        use_container_width=True,
    ):
        st.session_state["current_page"] = page_id
        if page_id == "skills_library":
            st.session_state["slib_edit_id"] = None
        st.rerun()
    if active:
        st.markdown("</div>", unsafe_allow_html=True)


def _render_marketplace_nav(page: str, lang: str) -> None:
    """Render the catalog subsections of the marketplace block.

    Every entry opens the same showcase page with its own subsection
    preselected; the page tabs write the same state key, so the sidebar
    highlight follows a tab click too.
    """
    current = active_marketplace_section()
    for section_key, _type, _kind, icon, label_key in _MARKETPLACE_SECTIONS:
        active = (page == "marketplace" and current == section_key)
        if active:
            st.markdown('<div class="nav-active">', unsafe_allow_html=True)
        if st.button(
            f"{icon} {t(label_key, lang=lang)}",
            key=f"nav_marketplace_{section_key}",
            use_container_width=True,
        ):
            # The showcase reads the shared section key for its highlight
            # and consumes the target key to open the matching tab: writing
            # the tab widget state itself is not allowed after creation.
            st.session_state["mp_section"] = section_key
            st.session_state["mp_section_target"] = section_key
            st.session_state["current_page"] = "marketplace"
            st.rerun()
        if active:
            st.markdown("</div>", unsafe_allow_html=True)


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


def _run_dependency_check_once():
    """Audit installed dependencies once per session at cold start.

    Warns about missing or outdated packages so the user can run
    ``pip install -r requirements.txt`` (or ``pip install -U <pkg>``) before
    hitting a confusing ImportError deep inside a feature. Runs at most
    once per session to keep reruns cheap; a probing error is swallowed and
    never blocks the app start.
    """
    if st.session_state.get("_deps_checked"):
        return
    st.session_state["_deps_checked"] = True
    try:
        report = check_dependencies()
    except Exception:
        return
    lang = st.session_state.get("ui_lang")
    missing = missing_packages(report)
    if missing:
        st.warning(t("deps_missing", lang=lang, libs=", ".join(missing)))
    outdated = outdated_lines(report)
    if outdated:
        st.warning(t("deps_outdated", lang=lang, libs=", ".join(outdated)))


def main():
    """Entry point called from app.py after st.set_page_config."""
    # ── Authentication gate ────────────────────────────────────────────────
    require_auth()

    # Seed built-in instructions and DevAgent settings.
    if not st.session_state.get("_defaults_seeded"):
        default_imports.ensure_all_defaults()
        # Entities that predate the catalog are adopted before anything reads
        # the registry: the bundled defaults create their folders above, and
        # the adoption turns those folders into registry rows. Without it an
        # existing installation would report "not installed" and offer a
        # second copy of what the user already has.
        if marketplace_enabled():
            try:
                from core.marketplace import migration as marketplace_migration

                marketplace_migration.migrate()
            except Exception:
                pass
        # Registry rows for skills installed before the marketplace existed.
        # Seeding is a local DB write only - it never touches the catalog
        # (the no-network test pins that), and a disabled marketplace is not
        # allowed to write anything at all.
        if marketplace_enabled():
            try:
                from core.marketplace import registry as marketplace_registry

                marketplace_registry.seed_local_orphans()
            except Exception:
                pass
        # One-time cleanup of leaked platform folders in foreign dialogs'
        # saved workspace (workspace isolation v2, see core.threads_devagent).
        # Idempotent; a failure here must never block the app start.
        try:
            migrate_platform_workspace_meta()
        except Exception:
            pass
        st.session_state["_defaults_seeded"] = True

    # Load env vars from shell profiles.
    load_env_from_shell_profiles()

    _run_dependency_check_once()

    defaults = dict(
        show_assistant_form=False,
        edit_assistant_id=None,
        show_skill_form=False,
        edit_skill_id=None,
        user_input_value="",
        force_send=False,
        active_thread_id=None,
        confirm_delete_all=False,
        attached_file_context="",
        attached_file_name="",
        input_key=0,
        current_page="welcome",
        # DevAgent session state
        devagent_workspace="",
        devagent_target_file="",
        devagent_thread_id=None,
        devagent_history=[],
        # Track last active entity for context-dependent "New dialog" button
        last_active_entity_type=None,  # "orchestrator" or "assistant"
        last_active_entity_id=None,    # slug for orchestrator, assistant_id for assistant
        # Recently used assistants
        recent_assistant_ids=[],
        recent_skill_ids=[],
        # Preselected assistant ID (set from sidebar)
        selected_assistant_id=None,
        selected_skill_id=None,
        # Search query for assistants filter
        assistant_search_query="",
        skill_search_query="",
        assistant_search_reset=0,
        # Skills library edit state
        slib_edit_id=None,
        # RAG storage state
        storage_show_create=False,
        storage_edit_slug=None,
        storage_confirm_delete=None,
        storage_test_search=None,
    )
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    _restore_ui_reload_state()
    _handle_thread_deeplink()

    langs      = get_langs()
    cfg        = load_config()
    lang_names = list(langs.keys())
    # Russian first
    if "\u0420\u0443\u0441\u0441\u043a\u0438\u0439" in lang_names:
        lang_names.remove("\u0420\u0443\u0441\u0441\u043a\u0438\u0439")
        lang_names.insert(0, "\u0420\u0443\u0441\u0441\u043a\u0438\u0439")
    if "ui_lang" not in st.session_state or st.session_state.ui_lang not in lang_names:
        saved = cfg.get("ui_lang", "")
        default_lang = get_default_ui_lang()
        if saved and saved in lang_names:
            st.session_state.ui_lang = saved
        elif default_lang and default_lang in lang_names:
            st.session_state.ui_lang = default_lang
        elif "\u0420\u0443\u0441\u0441\u043a\u0438\u0439" in lang_names:
            st.session_state.ui_lang = "\u0420\u0443\u0441\u0441\u043a\u0438\u0439"
        else:
            st.session_state.ui_lang = lang_names[0] if lang_names else ""

    lang = st.session_state.get("ui_lang")
    page = st.session_state["current_page"]

    # One-time localStorage sync for the saved theme: after a cold start the
    # sidebar shows the saved theme, but the page is painted from the
    # browser-local storage which is only written on manual switches.
    _sync_saved_theme_once(cfg)

    # ── Sidebar CSS ─────────────────────────────────────────────────────────
    st.markdown("""<style>
    section[data-testid="stSidebar"] hr {
        margin-top: 0.15rem !important;
        margin-bottom: 0.15rem !important;
        border: none !important;
        border-top: 1px solid rgba(128,128,128,0.4) !important;
    }
    section[data-testid="stSidebar"] .stButton {
        margin-bottom: 0.05rem !important;
    }
        section[data-testid="stSidebar"] .nav-active button{
        background:rgba(100,149,237,.18)!important;
        color:#7eb8f7!important;font-weight:600!important;
        border-radius:6px!important;}
    section[data-testid="stSidebar"] button{
        text-align:left!important;border-radius:6px!important;}
    /* Section headers */
    .sidebar-section-header {
        font-size:0.95rem;
        color:inherit !important;
        font-weight:700;
        letter-spacing:.04em;
        text-transform:uppercase;
        margin-top:16px;
        margin-bottom:6px;
        padding:8px 0;
        border-top:1px solid rgba(128,128,128,0.5);
        border-bottom:1px solid rgba(128,128,128,0.5);
    }
    /* Hide the default Streamlit uploader limit caption (e.g. "200MB per file") */
    div[data-testid="stFileUploaderDropzoneInstructions"] { display: none !important; }
    /* Sidebar footer: copyright and license link */
    .sidebar-footer {
        font-size:0.75rem;
        color:rgba(170,170,170,0.9);
        text-align:center;
        margin-top:6px;
    }
    .sidebar-footer a {
        color:#7eb8f7;
        text-decoration:none;
    }
    .sidebar-footer a:hover {
        text-decoration:underline;
    }
    </style>""", unsafe_allow_html=True)

    with st.sidebar:
        st.title(f"\U0001f916 {t('app_title', lang=lang)}")
        st.markdown("---")
        # ── New dialog button (context-dependent) ──────────────────────────
        entity_type = st.session_state.get("last_active_entity_type")
        entity_id   = st.session_state.get("last_active_entity_id")

        if entity_type == "orchestrator":
            new_dialog_label = f"\U0001f4ac {t('sidebar_new_dialog_agent', lang=lang)}"
        elif entity_type == "assistant":
            new_dialog_label = f"\U0001f4ac {t('sidebar_new_query', lang=lang)}"
        else:
            new_dialog_label = f"\U0001f4ac {t('sidebar_new_dialog', lang=lang)}"

        if st.button(
            new_dialog_label, key="sb_new",
            use_container_width=True,
            type="primary" if (st.session_state.get("active_thread_id") or page.startswith("orchestrator:")) else "secondary",
        ):
            if entity_type == "orchestrator" and entity_id:
                # Reset orchestrator state and start fresh chat
                st.session_state.active_thread_id         = None
                st.session_state["attached_file_context"] = ""
                st.session_state["attached_file_name"]    = ""
                st.session_state.force_send               = False
                st.session_state.devagent_workspace       = ""
                st.session_state.devagent_target_file     = ""
                st.session_state.devagent_thread_id       = None
                st.session_state.devagent_history         = []
                st.session_state["current_page"]          = f"orchestrator:{entity_id}"
                # Reset that orchestrator's state
                import ui.pages.orchestrator as _orch_page
                _orch_page._reset_dialog(entity_id)
                st.rerun()
            elif entity_type == "assistant" and entity_id:
                # Reset assistant chat and start fresh
                st.session_state.active_thread_id         = None
                st.session_state["attached_file_context"] = ""
                st.session_state["attached_file_name"]    = ""
                st.session_state.force_send               = False
                st.session_state["current_page"]          = "run"
                # Set the selected skill for page_run_query
                st.session_state["selected_skill_id"]     = entity_id
                record_assistant_use(entity_id)
                st.rerun()
            else:
                # Fallback: open DevAgent chat
                st.session_state.active_thread_id         = None
                st.session_state["attached_file_context"] = ""
                st.session_state["attached_file_name"]    = ""
                st.session_state.force_send               = False
                st.session_state.devagent_workspace       = ""
                st.session_state.devagent_target_file     = ""
                st.session_state.devagent_thread_id       = None
                st.session_state.devagent_history         = []
                st.session_state["current_page"]          = f"orchestrator:{DEVAGENT_SLUG}"
                import ui.pages.orchestrator as _orch_page
                _orch_page._reset_dialog(DEVAGENT_SLUG)
                st.rerun()

        # ── Active dialog indicator ────────────────────────────────────────
        active_tid = st.session_state.get("active_thread_id")
        active_orch_slug = None
        if page.startswith("orchestrator:") and not page.startswith("orchestrator_settings:"):
            active_orch_slug = page.split(":", 1)[1]

        # Show active chat info (compact)
        if active_tid:
            tmeta = load_thread_meta(active_tid)
            st.markdown(
                f'''<div style="background:#0d2137;border-radius:8px;
                padding:6px 10px;margin-bottom:8px">
                <div style="font-size:0.65rem;color:#7eb8f7;letter-spacing:.05em;
                text-transform:uppercase">{t("sidebar_active_dialog", lang=lang)}</div>
                <div style="font-size:0.8rem;color:#e0e0e0;font-weight:600;
                margin-top:1px">{(tmeta.get("title") or t("untitled", lang=lang))[:40]}</div>
                </div>''',
                unsafe_allow_html=True,
            )
        elif active_orch_slug:
            # Show which orchestrator is active even without a thread
            from core.orchestrators import get_orchestrator
            orch = get_orchestrator(active_orch_slug)
            orch_name = orch.get("name", active_orch_slug) if orch else active_orch_slug
            st.markdown(
                f'''<div style="background:#0d2137;border-radius:8px;
                padding:6px 10px;margin-bottom:8px">
                <div style="font-size:0.65rem;color:#7eb8f7;letter-spacing:.05em;
                text-transform:uppercase">{t("sidebar_active_agent", lang=lang)}</div>
                <div style="font-size:0.8rem;color:#e0e0e0;font-weight:600;
                margin-top:1px">{_DEVAGENT_ICON if active_orch_slug == DEVAGENT_SLUG else _ORCH_ICON} {orch_name}</div>
                </div>''',
                unsafe_allow_html=True,
            )

        # ── History button (right after New dialog / Active agent) ─────────
        is_history_active = (page == "history")
        if is_history_active:
            st.markdown('<div class="nav-active">', unsafe_allow_html=True)
        if st.button(
            f"\U0001f4dc {t('nav_history', lang=lang)}",
            key="nav_history",
            use_container_width=True,
        ):
            st.session_state["current_page"] = "history"
            st.rerun()
        if is_history_active:
            st.markdown("</div>", unsafe_allow_html=True)

        # ═══════════════════════════════════════════════════════════════════
        #  EMPLOYEES (Orchestrators)
        # ═══════════════════════════════════════════════════════════════════
        st.markdown(
            f'<div class="sidebar-section-header">{t("sidebar_section_employees", lang=lang)}</div>',
            unsafe_allow_html=True,
        )

        visible_orchs, collapsed_orchs = _build_orch_nav()
        total_orchs = len(visible_orchs) + len(collapsed_orchs)

        def _render_orch_button(page_id, label, slug):
            """Render one employee button together with its click handler."""
            is_active = (page == page_id)
            if is_active:
                st.markdown('<div class="nav-active">', unsafe_allow_html=True)
            if st.button(label, key=f"nav_{page_id}", use_container_width=True):
                st.session_state["last_active_entity_type"] = "orchestrator"
                st.session_state["last_active_entity_id"]   = slug
                st.session_state["current_page"] = page_id
                # Reset the employee search so the sidebar returns to the
                # grouped layout on the next render (same as assistants).
                st.session_state["orch_search_query"] = ""
                st.session_state["orch_search_reset"] = int(st.session_state.get("orch_search_reset", 0)) + 1
                # Reset orchestrator state for fresh entry
                import ui.pages.orchestrator as _orch_page
                _orch_page._reset_dialog(slug)
                st.rerun()
            if is_active:
                st.markdown("</div>", unsafe_allow_html=True)

        if total_orchs:
            # The search field appears only when there are more than five
            # employees (fixed block + collapsed remainder).
            orch_search_query = ""
            if total_orchs > DEFAULT_VISIBLE_ORCHESTRATORS:
                orch_search_query = st.text_input(
                    t("sidebar_search_employees", lang=lang),
                    value=st.session_state.get("orch_search_query", ""),
                    key=f"orch_search_input_{st.session_state.get('orch_search_reset', 0)}",
                    placeholder=t("sidebar_search_employees_placeholder", lang=lang),
                    label_visibility="collapsed",
                )
                st.session_state["orch_search_query"] = orch_search_query

            if orch_search_query:
                # Search mode: show every matching employee, no groups.
                all_matching = [
                    (page_id, label, slug)
                    for page_id, label, slug in (visible_orchs + collapsed_orchs)
                    if orch_search_query.lower() in label.lower()
                ]
                for page_id, label, slug in all_matching:
                    _render_orch_button(page_id, label, slug)
                if not all_matching:
                    st.caption(t("sidebar_no_employees_found", lang=lang))
            else:
                # Normal mode: fixed visible block, then collapsed "All".
                for page_id, label, slug in visible_orchs:
                    _render_orch_button(page_id, label, slug)
                if collapsed_orchs:
                    all_label = t("sidebar_all_employees", lang=lang, count=len(collapsed_orchs))
                    with st.expander(all_label, expanded=False):
                        for page_id, label, slug in collapsed_orchs:
                            _render_orch_button(page_id, label, slug)

        # ═══════════════════════════════════════════════════════════════════
        #  ASSISTANTS
        # ═══════════════════════════════════════════════════════════════════
        st.markdown(
            f'<div class="sidebar-section-header">{t("sidebar_section_assistants", lang=lang)}</div>',
            unsafe_allow_html=True,
        )

        visible_assistants, collapsed_assistants = _build_assistants_nav(lang)
        has_skills = bool(visible_assistants or collapsed_assistants)

        if has_skills:
            # Search field: shown only when there are more than five
            # assistants (fixed block + collapsed remainder) - the same
            # rule as for the employees section above.
            search_query = ""
            total_assistants = len(visible_assistants) + len(collapsed_assistants)
            if total_assistants > DEFAULT_VISIBLE_ASSISTANTS:
                search_query = st.text_input(
                    t("sidebar_search_skills", lang=lang),
                    value=st.session_state.get("assistant_search_query") or st.session_state.get("skill_search_query", ""),
                    key=f"assistant_search_input_{st.session_state.get('assistant_search_reset', 0)}",
                    placeholder=t("sidebar_search_placeholder", lang=lang),
                    label_visibility="collapsed",
                )
                st.session_state["assistant_search_query"] = search_query
                st.session_state["skill_search_query"] = search_query

            # Filter by search query
            def _matches_search(name: str) -> bool:
                if not search_query:
                    return True
                return search_query.lower() in name.lower()

            if search_query:
                # Search mode: show all matching assistants, no groups
                all_matching = [
                    (sid, name) for sid, name in (visible_assistants + collapsed_assistants)
                    if _matches_search(name)
                ]
                for sid, name in all_matching:
                    is_active = (
                        page == "run"
                        and (st.session_state.get("selected_assistant_id") or st.session_state.get("selected_skill_id")) == sid
                    )
                    label = f"{_ASSISTANT_ICON} {name}"
                    if is_active:
                        st.markdown('<div class="nav-active">', unsafe_allow_html=True)
                    if st.button(label, key=f"nav_assistant_{sid}", use_container_width=True):
                        record_assistant_use(sid)
                        st.session_state["last_active_entity_type"] = "assistant"
                        st.session_state["last_active_entity_id"]   = sid
                        st.session_state["selected_assistant_id"]   = sid
                        st.session_state["selected_skill_id"]        = sid
                        st.session_state["assistant_search_query"]   = ""
                        st.session_state["skill_search_query"]       = ""
                        st.session_state["assistant_search_reset"]   = int(st.session_state.get("assistant_search_reset", 0)) + 1
                        st.session_state["active_thread_id"]         = None
                        st.session_state["attached_file_context"]    = ""
                        st.session_state["attached_file_name"]       = ""
                        st.session_state["current_page"]             = "run"
                        st.rerun()
                    if is_active:
                        st.markdown("</div>", unsafe_allow_html=True)

                if not all_matching:
                    st.caption(t("sidebar_no_skills_found", lang=lang))
            else:
                # Normal mode: fixed visible block, then collapsed "All"
                selected_assistant_id = st.session_state.get("selected_assistant_id") or st.session_state.get("selected_skill_id")

                # Visible assistants (up to 5, newest activity first)
                if visible_assistants:
                    for sid, name in visible_assistants:
                        is_active = (page == "run" and selected_assistant_id == sid)
                        label = f"{_ASSISTANT_ICON} {name}"
                        if is_active:
                            st.markdown('<div class="nav-active">', unsafe_allow_html=True)
                        if st.button(label, key=f"nav_assistant_{sid}", use_container_width=True):
                            record_assistant_use(sid)
                            st.session_state["last_active_entity_type"] = "assistant"
                            st.session_state["last_active_entity_id"]   = sid
                            st.session_state["selected_assistant_id"]    = sid
                            st.session_state["selected_skill_id"]        = sid
                            st.session_state["assistant_search_query"]   = ""
                            st.session_state["skill_search_query"]       = ""
                            st.session_state["assistant_search_reset"]   = int(st.session_state.get("assistant_search_reset", 0)) + 1
                            st.session_state["active_thread_id"]         = None
                            st.session_state["attached_file_context"]    = ""
                            st.session_state["attached_file_name"]       = ""
                            st.session_state["current_page"]             = "run"
                            st.rerun()
                        if is_active:
                            st.markdown("</div>", unsafe_allow_html=True)

                # All remaining assistants (collapsed by default)
                if collapsed_assistants:
                    all_label = t("sidebar_all_skills", lang=lang, count=len(collapsed_assistants))
                    with st.expander(all_label, expanded=False):
                        for sid, name in collapsed_assistants:
                            is_active = (page == "run" and selected_assistant_id == sid)
                            label = f"{_ASSISTANT_ICON} {name}"
                            if is_active:
                                st.markdown('<div class="nav-active">', unsafe_allow_html=True)
                            if st.button(label, key=f"nav_assistant_{sid}", use_container_width=True):
                                record_assistant_use(sid)
                                st.session_state["last_active_entity_type"] = "assistant"
                                st.session_state["last_active_entity_id"]   = sid
                                st.session_state["selected_assistant_id"]    = sid
                                st.session_state["selected_skill_id"]        = sid
                                st.session_state["assistant_search_query"]   = ""
                                st.session_state["skill_search_query"]       = ""
                                st.session_state["assistant_search_reset"]   = int(st.session_state.get("assistant_search_reset", 0)) + 1
                                st.session_state["active_thread_id"]         = None
                                st.session_state["attached_file_context"]    = ""
                                st.session_state["attached_file_name"]       = ""
                                st.session_state["current_page"]             = "run"
                                st.rerun()
                            if is_active:
                                st.markdown("</div>", unsafe_allow_html=True)

        else:
            st.caption(t("sidebar_no_skills", lang=lang))

        st.markdown("---")

        # ═══════════════════════════════════════════════════════════════════
        #  MARKETPLACE (catalog subsections)
        # ═══════════════════════════════════════════════════════════════════
        if marketplace_enabled():
            st.markdown(
                f'<div class="sidebar-section-header">{t("sidebar_section_marketplace", lang=lang)}</div>',
                unsafe_allow_html=True,
            )
            _render_marketplace_nav(page, lang)

        st.markdown("---")

        # ═══════════════════════════════════════════════════════════════════
        #  SETTINGS
        # ═══════════════════════════════════════════════════════════════════
        st.markdown(
            f'<div class="sidebar-section-header">{t("sidebar_section_settings", lang=lang)}</div>',
            unsafe_allow_html=True,
        )

        # The three primary entries stay visible; everything else lives in
        # the collapsed block right below them, in a fixed order.
        for _page_id, _icon, _label_key in _SETTINGS_PRIMARY:
            _render_nav_button(_page_id, _icon, _label_key, page, lang)

        with st.expander(
            t("sidebar_more", lang=lang, count=len(_SETTINGS_MORE)), expanded=False
        ):
            for _page_id, _icon, _label_key in _SETTINGS_MORE:
                _render_nav_button(_page_id, _icon, _label_key, page, lang)

        st.markdown("---")

        # ═══════════════════════════════════════════════════════════════════
        #  LANGUAGE & THEME
        # ═══════════════════════════════════════════════════════════════════
        cur_idx = lang_names.index(st.session_state.ui_lang) if st.session_state.ui_lang in lang_names else 0
        selected_lang = st.selectbox(
            t("lang_label", lang=lang), lang_names, index=cur_idx, key="lang_selector"
        )
        if selected_lang != st.session_state.ui_lang:
            st.session_state.ui_lang = selected_lang
            new_cfg = dict(cfg)
            new_cfg["ui_lang"] = selected_lang
            save_config(new_cfg)
            st.rerun()

        theme_modes = _THEME_MODES
        current_theme = cfg.get("ui_theme", "System")
        if current_theme not in theme_modes:
            current_theme = "System"
        selected_theme = st.selectbox(
            t("theme_label", lang=lang),
            options=theme_modes,
            index=theme_modes.index(current_theme),
            format_func=lambda mode: t(f"theme_{mode.lower()}", lang=lang),
            key="theme_selector",
        )
        if selected_theme != current_theme:
            new_cfg = dict(cfg)
            new_cfg["ui_theme"] = selected_theme
            save_config(new_cfg)
            _apply_theme(selected_theme, _build_ui_restore_payload())

        # ── Sidebar footer: copyright and license link ───────────────────────
        st.markdown(
            f'<div class="sidebar-footer">© 2026 Deinekin T.V. | v{PLATFORM_VERSION} | '
            '<a href="https://github.com/TVD2100/sagaai-platform/blob/main/LICENSE" '
            'target="_blank" rel="noopener">PolyForm Noncommercial License</a></div>',
            unsafe_allow_html=True,
        )

    # ── Page dispatch ─────────────────────────────────────────────────────────
    if page == "welcome":
        page_welcome()
    elif page == "run":
        page_run_query()
    elif page == "history":
        page_history()
    elif page == "skills":
        page_skills()
    elif page == "skills_library":
        page_skills_library()
    elif page == "marketplace":
        page_marketplace()
    elif page == "storage":
        page_storage()
    elif page == "connectors":
        page_connectors()
    elif page == "stats":
        page_stats()
    elif page == "orchestrators":
        page_orchestrators()
    elif page.startswith("orchestrator_settings:"):
        slug = page.split(":", 1)[1]
        page_orchestrator_settings(slug)
    elif page.startswith("orchestrator:"):
        slug = page.split(":", 1)[1]
        page_orchestrator(slug)
    elif page == "updates":
        page_updates()
    elif page == "access":
        page_access()
    elif page == "settings":
        page_settings()
    else:
        # Fallback for legacy "devagent" page value.
        page_orchestrator(DEVAGENT_SLUG)

    # Queued notifications are painted here, once, AFTER the page body: a
    # page that saves something and calls st.rerun() keeps its confirmation
    # visible on the next pass, below the content of the page it belongs to
    # instead of above its title. The queue survives the discarded render.
    render_flash()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
