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
import streamlit as st

import core.default_imports as default_imports
from core.assistant_nav import DEFAULT_VISIBLE_ASSISTANTS, sort_assistants, split_nav_lists
from core.assistants import load_assistants_index
from core.auth import require_auth
from core.config import get_default_ui_lang, load_config, save_config
from core.dependency_check import (
    check_dependencies,
    missing_packages,
    outdated_lines,
)
from core.env_loader import load_env_from_shell_profiles
from core.i18n import get_langs, t
from core.marketplace import is_enabled as marketplace_enabled
from core.orchestrator_nav import DEFAULT_VISIBLE_ORCHESTRATORS, sort_orchestrators
from core.orchestrators import DEVAGENT_SLUG, list_orchestrators
from core.recent_assistants import record_assistant_use
from core.threads import list_chat_threads, load_thread_meta
from core.threads_devagent import list_devagent_threads, migrate_platform_workspace_meta
from core.version import __version__ as PLATFORM_VERSION

# Theme/restore helpers live in ui/app_restore.py and are re-exported here so
# existing callers and tests keep addressing them through the app module.
from ui.app_restore import (  # noqa: F401
    _THEME_MODES,
    _apply_theme,
    _build_ui_restore_payload,
    _handle_thread_deeplink,
    _restore_ui_reload_state,
    _sync_saved_theme,
    _sync_saved_theme_once,
)
from ui.components.flash import flash_warning, render_flash
from ui.pages.access import page_access
from ui.pages.chat import page_run_query
from ui.pages.connectors import page_connectors
from ui.pages.history import page_history
from ui.pages.marketplace import page_marketplace
from ui.pages.orchestrator import page_orchestrator
from ui.pages.orchestrator_settings import page_orchestrator_settings
from ui.pages.orchestrators import page_orchestrators
from ui.pages.schedules import page_schedules
from ui.pages.settings import page_settings
from ui.pages.skills import page_skills
from ui.pages.skills_library import page_skills_library
from ui.pages.stats import page_stats
from ui.pages.storage import page_storage
from ui.pages.updates import page_updates
from ui.pages.welcome import page_welcome

# ── Icons ─────────────────────────────────────────────────────────────────────
_ORCH_ICON = "\U0001f916"          # custom orchestrators: robot
_DEVAGENT_ICON = "\U0001f6e0\ufe0f"      # built-in DevAgent: wrench + hammer
_ASSISTANT_ICON = "\U0001f9e9"      # assistants
_SLIB_ICON = "\U0001f9e0"          # skills library: puzzle piece
_MARKETPLACE_ICON = "\U0001f6d2"    # marketplace: shopping trolley
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
    ("schedules", "\u23f0", "nav_schedules"),
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




def _run_dependency_check_once():
    """Audit installed dependencies once per session at cold start.

    Warns about missing or outdated packages so the user can reinstall the
    pinned set from the lock (``pip install -r requirements.lock``) or update
    a single package (``pip install -U <pkg>``) before hitting a confusing
    ImportError deep inside a feature. Runs at most once per session to keep
    reruns cheap; a probing error is swallowed and never blocks the app
    start.
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


def _show_db_archive_notice_once(lang) -> None:
    """Tell the user once that the previous database was archived.

    A database written before the transcript file store held every dialogue
    inside itself. It is replaced by an empty one, and the old file is kept
    as a zip beside it - the message has to say so, because the dialogue list
    is empty afterwards and silence would read as data loss.

    The notice is taken exactly once: it is written at cold start, and a
    rerun must keep showing it. The read-and-clear runs once per session, so
    the first render of every browser announces the switch.
    """
    if st.session_state.get("_db_archive_notice_shown"):
        return
    st.session_state["_db_archive_notice_shown"] = True
    try:
        from core import devagent_db_archive as _archive

        notice = _archive.take_notice()
    except Exception:
        return
    if not notice:
        return
    flash_warning(t(
        "devagent_db_archived",
        lang=lang,
        path=notice.get("archive_path", ""),
        threads=int(notice.get("threads") or 0),
        messages=int(notice.get("messages") or 0),
    ))


def _show_chat_db_archive_notice_once(lang) -> None:
    """Tell the user once that assistant dialogues moved to files.

    Assistant messages used to live inside ``sagaai.db``. They are now written
    to one transcript file per dialogue, and the previous database is kept as
    a zip beside itself. The message has to say so: the table is empty
    afterwards, and a user opening an old dialogue deserves to know where the
    previous copy is and that nothing else was touched.

    Unlike the DevAgent database, the main database is not replaced, so the
    archive is a safety net rather than the only copy of the old data. The
    notice is taken exactly once per browser session: it is written at cold
    start, and every rerun of the same session must keep showing it.
    """
    if st.session_state.get("_chat_db_archive_notice_shown"):
        return
    st.session_state["_chat_db_archive_notice_shown"] = True
    try:
        from core import chat_db_archive as _archive

        notice = _archive.take_notice()
    except Exception:
        return
    if not notice:
        return
    flash_warning(t(
        "chat_db_archived",
        lang=lang,
        path=notice.get("archive_path", ""),
        threads=int(notice.get("threads") or 0),
        messages=int(notice.get("messages") or 0),
    ))


def _seed_defaults_once() -> None:
    """Seed bundled defaults and the marketplace registry once per session.

    The flag is set at the END: a failure above must not mark the session as
    seeded, or the next render would skip the seeding entirely.
    """
    if st.session_state.get("_defaults_seeded"):
        return
    default_imports.ensure_all_defaults()
    _adopt_preexisting_entities()
    # One-time cleanup of leaked platform folders in foreign dialogs'
    # saved workspace (workspace isolation v2, see core.threads_devagent).
    # Idempotent; a failure here must never block the app start.
    try:
        migrate_platform_workspace_meta()
    except Exception:
        pass
    st.session_state["_defaults_seeded"] = True


def _adopt_preexisting_entities() -> None:
    """Adopt entities that predate the marketplace catalog.

    The bundled defaults create their folders before anything reads the
    registry, and the adoption turns those folders into registry rows.
    Without it an existing installation would report "not installed" and
    offer a second copy of what the user already has. Seeding is a local DB
    write only - it never touches the catalog (the no-network test pins
    that), and a disabled marketplace is not allowed to write anything.
    """
    if not marketplace_enabled():
        return
    try:
        from core.marketplace import migration as marketplace_migration

        marketplace_migration.migrate()
    except Exception:
        pass
    try:
        from core.marketplace import registry as marketplace_registry

        marketplace_registry.seed_local_orphans()
    except Exception:
        pass


def _session_defaults() -> dict:
    """Return the session-state defaults of a fresh dialog.

    Only keys ABSENT from the session are filled in, so a value set before
    the first render (a deep link, a test fixture) always wins.
    """
    return dict(
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


def _resolve_languages() -> tuple:
    """Return (langs, cfg, lang_names) with Russian placed first."""
    langs = get_langs()
    cfg = load_config()
    lang_names = list(langs.keys())
    if "\u0420\u0443\u0441\u0441\u043a\u0438\u0439" in lang_names:
        lang_names.remove("\u0420\u0443\u0441\u0441\u043a\u0438\u0439")
        lang_names.insert(0, "\u0420\u0443\u0441\u0441\u043a\u0438\u0439")
    return langs, cfg, lang_names


def _ensure_ui_lang(cfg: dict, lang_names: list) -> None:
    """Pick the session language: stored choice, then the default, then Russian.

    A stored value outside the available languages is replaced, so a removed
    language file cannot leave the session pointing at nothing.
    """
    if "ui_lang" in st.session_state and st.session_state.ui_lang in lang_names:
        return
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


def _render_sidebar_styles() -> None:
    """Install the sidebar CSS: separators, active items, headers, footer."""
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


def _dispatch_page(page: str) -> None:
    """Render the page the sidebar selected.

    ``page`` is a bare name (``\"history\"``) or a value with a payload
    (``\"orchestrator:<slug>\"`` / ``\"orchestrator_settings:<slug>\"``). The
    settings branch is checked first: ``startswith(\"orchestrator\")`` would
    otherwise swallow it. An unknown value falls back to the DevAgent chat,
    which keeps a legacy ``\"devagent\"`` page working.
    """
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
    elif page == "schedules":
        page_schedules()
    elif page == "connectors":
        page_connectors()
    elif page == "stats":
        page_stats()
    elif page == "orchestrators":
        page_orchestrators()
    elif page.startswith("orchestrator_settings:"):
        page_orchestrator_settings(page.split(":", 1)[1])
    elif page.startswith("orchestrator:"):
        page_orchestrator(page.split(":", 1)[1])
    elif page == "updates":
        page_updates()
    elif page == "access":
        page_access()
    elif page == "settings":
        page_settings()
    else:
        page_orchestrator(DEVAGENT_SLUG)


def _assistant_button(sid: str, name: str, page: str, is_active: bool) -> None:
    """Render one assistant entry and its click handler.

    The three places that list assistants (search results, the fixed block
    and the collapsed remainder) share this one handler, so a change to the
    reset sequence cannot drift between them.
    """
    label = f"{_ASSISTANT_ICON} {name}"
    if is_active:
        st.markdown('<div class="nav-active">', unsafe_allow_html=True)
    if st.button(label, key=f"nav_assistant_{sid}", use_container_width=True):
        record_assistant_use(sid)
        st.session_state["last_active_entity_type"] = "assistant"
        st.session_state["last_active_entity_id"] = sid
        st.session_state["selected_assistant_id"] = sid
        st.session_state["selected_skill_id"] = sid
        st.session_state["assistant_search_query"] = ""
        st.session_state["skill_search_query"] = ""
        st.session_state["assistant_search_reset"] = int(st.session_state.get("assistant_search_reset", 0)) + 1
        st.session_state["active_thread_id"] = None
        st.session_state["attached_file_context"] = ""
        st.session_state["attached_file_name"] = ""
        st.session_state["current_page"] = "run"
        st.rerun()
    if is_active:
        st.markdown("</div>", unsafe_allow_html=True)


def _render_assistants_section(visible: list, collapsed: list, page: str, lang: str) -> None:
    """Render the assistants block: search field, fixed entries and the rest.

    The search field appears only above DEFAULT_VISIBLE_ASSISTANTS entries -
    the same rule the employees section uses. A search query switches the
    block to a flat list of matches with no groups.
    """
    search_query = ""
    total = len(visible) + len(collapsed)
    if total > DEFAULT_VISIBLE_ASSISTANTS:
        search_query = st.text_input(
            t("sidebar_search_skills", lang=lang),
            value=st.session_state.get("assistant_search_query") or st.session_state.get("skill_search_query", ""),
            key=f"assistant_search_input_{st.session_state.get('assistant_search_reset', 0)}",
            placeholder=t("sidebar_search_placeholder", lang=lang),
            label_visibility="collapsed",
        )
        st.session_state["assistant_search_query"] = search_query
        st.session_state["skill_search_query"] = search_query

    if search_query:
        matches = [
            (sid, name) for sid, name in (visible + collapsed)
            if search_query.lower() in name.lower()
        ]
        current = st.session_state.get("selected_assistant_id") or st.session_state.get("selected_skill_id")
        for sid, name in matches:
            _assistant_button(sid, name, page, page == "run" and current == sid)
        if not matches:
            st.caption(t("sidebar_no_skills_found", lang=lang))
        return

    selected_assistant_id = st.session_state.get("selected_assistant_id") or st.session_state.get("selected_skill_id")
    for sid, name in visible:
        _assistant_button(sid, name, page, page == "run" and selected_assistant_id == sid)
    if collapsed:
        all_label = t("sidebar_all_skills", lang=lang, count=len(collapsed))
        with st.expander(all_label, expanded=False):
            for sid, name in collapsed:
                _assistant_button(sid, name, page, page == "run" and selected_assistant_id == sid)


def _render_new_dialog_button(page: str, lang: str) -> None:
    """Render the top "new dialog" button with its context-dependent reset.

    The label and the reset target follow the last active entity: an
    orchestrator reopens its own chat, an assistant clears the assistant
    chat, and anything else falls back to the DevAgent chat.
    """
    entity_type = st.session_state.get("last_active_entity_type")
    entity_id = st.session_state.get("last_active_entity_id")

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
            _reset_orchestrator_chat(entity_id)
        elif entity_type == "assistant" and entity_id:
            _reset_assistant_chat(entity_id)
        else:
            _reset_orchestrator_chat(DEVAGENT_SLUG)


def _reset_orchestrator_chat(slug: str) -> None:
    """Clear the chat state and reopen the chat of one orchestrator."""
    st.session_state.active_thread_id = None
    st.session_state["attached_file_context"] = ""
    st.session_state["attached_file_name"] = ""
    st.session_state.force_send = False
    st.session_state.devagent_workspace = ""
    st.session_state.devagent_target_file = ""
    st.session_state.devagent_thread_id = None
    st.session_state.devagent_history = []
    st.session_state["current_page"] = f"orchestrator:{slug}"
    import ui.pages.orchestrator as _orch_page

    _orch_page._reset_dialog(slug)
    st.rerun()


def _reset_assistant_chat(entity_id: str) -> None:
    """Clear the chat state and open a fresh assistant query."""
    st.session_state.active_thread_id = None
    st.session_state["attached_file_context"] = ""
    st.session_state["attached_file_name"] = ""
    st.session_state.force_send = False
    st.session_state["current_page"] = "run"
    st.session_state["selected_skill_id"] = entity_id
    record_assistant_use(entity_id)
    st.rerun()


def _render_active_dialog_indicator(page: str, lang: str) -> None:
    """Render the compact "current dialog" / "active employee" badge.

    A bound thread wins over the bare page: its title identifies the dialog,
    while a page without a thread can only show which employee is open.
    """
    active_tid = st.session_state.get("active_thread_id")
    active_orch_slug = None
    if page.startswith("orchestrator:") and not page.startswith("orchestrator_settings:"):
        active_orch_slug = page.split(":", 1)[1]

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


def _render_history_button(page: str, lang: str) -> None:
    """Render the history entry, highlighted while the history page is open."""
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


def _render_employee_button(page_id, label, slug, page):
    """Render one employee button together with its click handler."""
    is_active = (page == page_id)
    if is_active:
        st.markdown('<div class="nav-active">', unsafe_allow_html=True)
    if st.button(label, key=f"nav_{page_id}", use_container_width=True):
        st.session_state["last_active_entity_type"] = "orchestrator"
        st.session_state["last_active_entity_id"] = slug
        st.session_state["current_page"] = page_id
        # Reset the employee search so the sidebar returns to the
        # grouped layout on the next render (same as assistants).
        st.session_state["orch_search_query"] = ""
        st.session_state["orch_search_reset"] = int(st.session_state.get("orch_search_reset", 0)) + 1
        import ui.pages.orchestrator as _orch_page

        _orch_page._reset_dialog(slug)
        st.rerun()
    if is_active:
        st.markdown("</div>", unsafe_allow_html=True)


def _render_employees_section(page: str, lang: str) -> None:
    """Render the employee navigation: search field, fixed entries, the rest.

    The search field appears only above DEFAULT_VISIBLE_ORCHESTRATORS entries
    (fixed block + collapsed remainder); a query switches the block to a flat
    list of matches with no groups.
    """
    st.markdown(
        f'<div class="sidebar-section-header">{t("sidebar_section_employees", lang=lang)}</div>',
        unsafe_allow_html=True,
    )

    visible_orchs, collapsed_orchs = _build_orch_nav()
    total_orchs = len(visible_orchs) + len(collapsed_orchs)

    if total_orchs:
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
            all_matching = [
                (page_id, label, slug)
                for page_id, label, slug in (visible_orchs + collapsed_orchs)
                if orch_search_query.lower() in label.lower()
            ]
            for page_id, label, slug in all_matching:
                _render_employee_button(page_id, label, slug, page)
            if not all_matching:
                st.caption(t("sidebar_no_employees_found", lang=lang))
        else:
            for page_id, label, slug in visible_orchs:
                _render_employee_button(page_id, label, slug, page)
            if collapsed_orchs:
                all_label = t("sidebar_all_employees", lang=lang, count=len(collapsed_orchs))
                with st.expander(all_label, expanded=False):
                    for page_id, label, slug in collapsed_orchs:
                        _render_employee_button(page_id, label, slug, page)


def _render_settings_section(page: str, lang: str) -> None:
    """Render the settings header and its two navigation groups.

    The three primary entries stay visible; everything else lives in the
    collapsed block right below them, in a fixed order.
    """
    st.markdown(
        f'<div class="sidebar-section-header">{t("sidebar_section_settings", lang=lang)}</div>',
        unsafe_allow_html=True,
    )
    for _page_id, _icon, _label_key in _SETTINGS_PRIMARY:
        _render_nav_button(_page_id, _icon, _label_key, page, lang)
    with st.expander(
        t("sidebar_more", lang=lang, count=len(_SETTINGS_MORE)), expanded=False
    ):
        for _page_id, _icon, _label_key in _SETTINGS_MORE:
            _render_nav_button(_page_id, _icon, _label_key, page, lang)


def _render_language_and_theme(lang: str, cfg: dict, lang_names: list) -> None:
    """Render the language and theme selectors and apply a change at once.

    A language change is stored and reruns the app; a theme change is stored
    and applied in place, so the palette switches without an extra rerun.
    """
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


def _render_sidebar_footer() -> None:
    """Render the footer with the copyright, version and license link."""
    st.markdown(
        f'<div class="sidebar-footer">© 2026 Deinekin T.V. | v{PLATFORM_VERSION} | '
        '<a href="https://github.com/TVD2100/sagaai-platform/blob/main/LICENSE" '
        'target="_blank" rel="noopener">PolyForm Noncommercial License</a></div>',
        unsafe_allow_html=True,
    )


def _render_sidebar(page: str, lang: str, cfg: dict, lang_names: list) -> None:
    """Render the whole sidebar: navigation, sections, language and theme.

    The block order IS the widget order, and three smoke tests pin it:
    settings entries come before the language selector, the language
    selector before the theme selector.
    """
    with st.sidebar:
        st.title(f"\U0001f916 {t('app_title', lang=lang)}")
        st.markdown("---")
        _render_new_dialog_button(page, lang)

        _render_active_dialog_indicator(page, lang)

        _render_history_button(page, lang)

        _render_employees_section(page, lang)

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
            _render_assistants_section(visible_assistants, collapsed_assistants, page, lang)
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

        _render_settings_section(page, lang)

        st.markdown("---")

        _render_language_and_theme(lang, cfg, lang_names)

        _render_sidebar_footer()


def main():
    """Entry point called from app.py after st.set_page_config."""
    # ── Authentication gate ────────────────────────────────────────────────
    require_auth()

    # Seed built-in instructions and DevAgent settings (once per session).
    _seed_defaults_once()

    # Load env vars from shell profiles.
    load_env_from_shell_profiles()

    _run_dependency_check_once()

    defaults = _session_defaults()
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    _restore_ui_reload_state()
    _handle_thread_deeplink()

    langs, cfg, lang_names = _resolve_languages()
    _ensure_ui_lang(cfg, lang_names)

    lang = st.session_state.get("ui_lang")
    page = st.session_state["current_page"]

    _show_db_archive_notice_once(lang)
    _show_chat_db_archive_notice_once(lang)

    # One-time localStorage sync for the saved theme: after a cold start the
    # sidebar shows the saved theme, but the page is painted from the
    # browser-local storage which is only written on manual switches.
    _sync_saved_theme_once(cfg)

    _render_sidebar_styles()

    _render_sidebar(page, lang, cfg, lang_names)

    # ── Page dispatch ─────────────────────────────────────────────────────────
    _dispatch_page(page)

    # Queued notifications are painted here, once, AFTER the page body: a
    # page that saves something and calls st.rerun() keeps its confirmation
    # visible on the next pass, below the content of the page it belongs to
    # instead of above its title. The queue survives the discarded render.
    render_flash()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
