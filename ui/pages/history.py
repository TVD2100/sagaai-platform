# -*- coding: utf-8 -*-
"""
ui.pages.history - unified dialogue history page.

Displays conversations from both assistants (skills) and employees
(orchestrators) in a single list with filtering by type, selecting a
specific employee, and searching by keyword in titles and messages.

Opening an orchestrator thread goes through ui.pages.orchestrator._load_thread
so the dialog's saved workspace / target file is restored together with the
message history: the history page and the orchestrator page's own History tab
now behave identically.
"""
import os
import streamlit as st

from core.i18n import t
from core.threads import (
    list_chat_threads, load_thread_messages,
    get_thread_dir, delete_thread as delete_chat_thread,
)
from core.threads_devagent import (
    list_devagent_threads, load_thread_messages as load_orch_thread_messages,
    delete_thread as delete_orch_thread,
)
from core.orchestrators import list_orchestrators, DEVAGENT_SLUG


def _active_orch_thread_id() -> str:
    """Return the id of the orchestrator dialog currently loaded in chat.

    The per-orchestrator session key ``orch_<slug>_thread_id`` is the source
    of truth; the slug of the last used orchestrator is tracked by the
    sidebar in ``last_active_entity_id``.  The legacy global key
    ``devagent_thread_id`` (set before this helper existed) is used as a
    fallback.
    """
    if st.session_state.get("last_active_entity_type") == "orchestrator":
        slug = st.session_state.get("last_active_entity_id") or ""
        if slug:
            tid = st.session_state.get(f"orch_{slug}_thread_id")
            if tid:
                return str(tid)
    return str(st.session_state.get("devagent_thread_id") or "")


def _clear_all_orch_markers() -> None:
    """Clear all per-orchestrator and legacy thread markers.

    Used after "delete all" so no deleted dialog stays flagged as active.
    """
    st.session_state["devagent_thread_id"] = None
    st.session_state["devagent_history"] = []
    for key in list(st.session_state.keys()):
        if key.startswith("orch_") and key.endswith("_thread_id"):
            st.session_state[key] = None
            st.session_state[key[: -len("_thread_id")] + "_history"] = []


def _clear_active_orch_state(tids) -> None:
    """Clear session markers of the given orchestrator thread ids."""
    if (st.session_state.get("devagent_thread_id") or "") in tids:
        st.session_state["devagent_thread_id"] = None
        st.session_state["devagent_history"] = []
    for key in list(st.session_state.keys()):
        if key.startswith("orch_") and key.endswith("_thread_id"):
            tid = st.session_state.get(key)
            if tid in tids:
                st.session_state[key] = None
                st.session_state[key[: -len("_thread_id")] + "_history"] = []


def _delete_all_chat_threads() -> None:
    """Delete all chat (assistant-based) threads."""
    threads = list_chat_threads()
    for meta in threads:
        tid = meta.get("thread_id", "")
        if tid:
            delete_chat_thread(tid)


def _delete_all_orch_threads(slug: str = None) -> None:
    """Delete all devagent threads, optionally filtered by orchestrator slug."""
    threads = list_devagent_threads(slug)
    for meta in threads:
        tid = meta.get("thread_id", "")
        if tid:
            delete_orch_thread(tid)


def page_history():
    """History page - list all threads (chat + devagent), open, delete."""
    lang = st.session_state.get("ui_lang")
    st.title(t("history_title", lang=lang))

    # ── Gather data ─────────────────────────────────────────────────────────
    chat_threads = list_chat_threads()
    orch_threads = list_devagent_threads()  # ALL orchestrator threads
    orchestrators = list_orchestrators()
    orch_map = {o["slug"]: o for o in orchestrators if o.get("slug")}

    # ── Filters ─────────────────────────────────────────────────────────────
    col_filter, col_search = st.columns([2, 3])

    with col_filter:
        filter_options = [
            t("history_filter_all", lang=lang),
            t("history_filter_assistants", lang=lang),
            t("history_filter_employees", lang=lang),
        ]
        selected_filter = st.selectbox(
            t("history_filter_label", lang=lang),
            options=filter_options,
            key="history_filter_type",
            label_visibility="collapsed",
        )

        show_assistants = selected_filter == filter_options[0] or selected_filter == filter_options[1]
        show_orchs = selected_filter == filter_options[0] or selected_filter == filter_options[2]

    # Sub-select: which orchestrator?
    selected_orch = None
    if show_orchs:
        orch_slugs = [(o["slug"], o.get("name", o["slug"])) for o in orchestrators if o.get("slug")]
        if orch_slugs:
            if selected_filter == filter_options[2]:
                # For "Employees only" filter, show selector inline
                with col_filter:
                    orch_names = [t("history_filter_all_employees", lang=lang)] + [name for _, name in orch_slugs]
                    selected_orch_name = st.selectbox(
                        t("history_filter_employee", lang=lang),
                        options=orch_names,
                        key="history_filter_orch",
                        label_visibility="collapsed",
                    )
                    if selected_orch_name == orch_names[0]:
                        selected_orch = None
                    else:
                        idx = orch_names.index(selected_orch_name) - 1
                        selected_orch = orch_slugs[idx][0] if idx >= 0 else None
            else:
                selected_orch = None  # show all employees when "All" is selected

    with col_search:
        search_query = st.text_input(
            t("history_search_placeholder", lang=lang),
            value=st.session_state.get("history_search_query", ""),
            key="history_search_input",
            label_visibility="collapsed",
        ).strip().lower()
        st.session_state["history_search_query"] = search_query

    # ── Build unified list ──────────────────────────────────────────────────
    all_threads = []

    if show_assistants:
        for ct in chat_threads:
            tid = ct.get("thread_id", "")
            title = ct.get("title") or t("untitled", lang=lang)
            msgs = load_thread_messages(tid) if tid else []
            # Check search
            if search_query:
                found = search_query in title.lower()
                if not found:
                    for m in msgs:
                        if search_query in (m.get("content") or "").lower():
                            found = True
                            break
                if not found:
                    continue
            all_threads.append({
                "thread_id": tid,
                "title": title,
                "entity_name": ct.get("assistant_name") or ct.get("skill_name") or "?",
                "entity_type": "assistant",
                "entity_slug": None,
                "updated_at": ct.get("updated_at", ""),
                "msg_count": len(msgs),
                "last_reply": _last_reply(msgs),
            })

    if show_orchs:
        for ot in orch_threads:
            tid = ot.get("thread_id", "")
            # assistant_id stores the orchestrator slug
            slug = ot.get("assistant_id") or ot.get("skill_id") or ""
            if selected_orch and slug != selected_orch:
                continue
            title = ot.get("title") or t("untitled", lang=lang)
            msgs = load_orch_thread_messages(tid) if tid else []
            # Check search
            if search_query:
                found = search_query in title.lower()
                if not found:
                    for m in msgs:
                        if search_query in (m.get("content") or "").lower():
                            found = True
                            break
                if not found:
                    continue
            # Determine name from orchestrators or from thread metadata
            if slug and slug in orch_map:
                entity_name = orch_map[slug].get("name", slug)
            else:
                entity_name = ot.get("assistant_name") or ot.get("skill_name") or slug or t("orch_untitled", lang=lang)
            all_threads.append({
                "thread_id": tid,
                "title": title,
                "entity_name": entity_name,
                "entity_type": "orchestrator",
                "entity_slug": slug,
                "updated_at": ot.get("updated_at", ""),
                "msg_count": len(msgs),
                "last_reply": _last_reply(msgs),
            })

    # Sort by updated_at desc
    all_threads.sort(key=lambda x: x["updated_at"] or "", reverse=True)

    if not all_threads:
        st.info(t("history_empty", lang=lang))
        return

    # ── Header with delete-all ──────────────────────────────────────────────
    col_h, col_del_all = st.columns([6, 2])
    with col_h:
        st.caption(t("history_total", lang=lang, count=len(all_threads)))
    with col_del_all:
        if st.button(t("btn_delete_all", lang=lang), use_container_width=True,
                     key="hist_delete_all_btn"):
            st.session_state["confirm_delete_all"] = True

    if st.session_state.get("confirm_delete_all"):
        with st.container(border=True):
            st.warning(t("confirm_delete_all_warn", lang=lang))
            c1, c2 = st.columns(2)
            with c1:
                if st.button(t("btn_yes_delete", lang=lang), key="confirm_yes_all",
                             type="primary"):
                    if show_assistants:
                        _delete_all_chat_threads()
                        st.session_state.active_thread_id = None
                    if show_orchs:
                        _delete_all_orch_threads(selected_orch)
                        _clear_all_orch_markers()
                    st.session_state["confirm_delete_all"] = False
                    st.rerun()
            with c2:
                if st.button(t("btn_cancel", lang=lang), key="confirm_no_all"):
                    st.session_state["confirm_delete_all"] = False
                    st.rerun()

    st.markdown("---")

    # ── Thread cards ────────────────────────────────────────────────────────
    for item in all_threads:
        tid = item["thread_id"]
        title = item["title"]
        entity_name = item["entity_name"]
        entity_type = item["entity_type"]
        entity_slug = item["entity_slug"]
        updated = (item["updated_at"] or "")[:16].replace("T", " ")
        msg_count = item["msg_count"]
        last_reply = item["last_reply"]

        # Determine active state
        is_chat = entity_type == "assistant"
        active_tid = st.session_state.get("active_thread_id")
        active_orch_tid = _active_orch_thread_id()

        if is_chat:
            is_active = (active_tid == tid)
        else:
            is_active = (active_orch_tid == tid)

        # Badge styling
        if entity_type == "assistant":
            type_badge = (
                '🔧'  # same wrench as current history page
            )
            type_label = t("history_type_assistant", lang=lang)
        else:
            # DevAgent gets its own icon
            if entity_slug == DEVAGENT_SLUG:
                type_badge = "🛠️"
            else:
                type_badge = "🤖"
            type_label = t("history_type_employee", lang=lang)

        with st.container(border=True):
            if is_active:
                st.markdown(
                    '<div style="position:absolute;width:4px;height:100%;'
                    'background:#0066cc;left:0;top:0;border-radius:4px 0 0 4px"></div>',
                    unsafe_allow_html=True,
                )

            col_info, col_open, col_del = st.columns([7, 2, 1])
            with col_info:
                active_badge = (
                    " &nbsp;<span style='background:#0066cc;color:white;font-size:0.7rem;"
                    "padding:1px 6px;border-radius:10px'>" + t("active_badge", lang=lang) + "</span>"
                    if is_active else ""
                )
                st.markdown(
                    f'''<div style="font-size:0.95rem;font-weight:600">{title[:70]}{active_badge}</div>''',
                    unsafe_allow_html=True,
                )
                detail = f"{type_badge} {entity_name} &nbsp;·&nbsp; 💬 {msg_count}"
                detail += f" &nbsp;·&nbsp; 🕐 {updated}"
                st.markdown(
                    f'<div style="font-size:0.78rem;color:#888;margin:2px 0 4px">{detail}</div>',
                    unsafe_allow_html=True,
                )
                if last_reply:
                    st.markdown(
                        f'<div style="font-size:0.8rem;color:#aaa;font-style:italic">{last_reply}…</div>',
                        unsafe_allow_html=True,
                    )
            with col_open:
                btn_label = t("btn_continue", lang=lang) if is_active else t("btn_open", lang=lang)
                if st.button(btn_label, key=f"open_hist_{tid}", use_container_width=True,
                             type="primary" if is_active else "secondary"):
                    if is_chat:
                        # Open in chat page
                        st.session_state.active_thread_id = tid
                        st.session_state.user_input_value = ""
                        st.session_state["attached_file_context"] = ""
                        st.session_state["attached_file_name"] = ""
                        st.session_state["selected_assistant_id"] = None
                        st.session_state["selected_skill_id"] = None
                        st.session_state["current_page"] = "run"
                    else:
                        # Open on the orchestrator page.  Use the shared
                        # loader so the dialog's saved workspace / target
                        # file is restored exactly like on the orchestrator
                        # page's own History tab.
                        import ui.pages.orchestrator as _orch_page
                        slug = entity_slug or ""
                        _orch_page._init_orch_state(slug)
                        _orch_page._load_thread(slug, tid)
                        st.session_state["devagent_thread_id"] = tid
                        st.session_state["devagent_history"] = _orch_page._ss(slug, "history") or []
                        st.session_state["last_active_entity_type"] = "orchestrator"
                        st.session_state["last_active_entity_id"] = slug
                        st.session_state["current_page"] = f"orchestrator:{slug}"
                    st.session_state["history_search_query"] = ""
                    st.rerun()
            with col_del:
                if st.button("✕", key=f"del_hist_{tid}",
                             help=t("delete_thread_help", lang=lang)):
                    if is_chat:
                        if st.session_state.get("active_thread_id") == tid:
                            st.session_state.active_thread_id = None
                        delete_chat_thread(tid)
                    else:
                        _clear_active_orch_state({tid})
                        delete_orch_thread(tid)
                    st.rerun()


def _last_reply(msgs: list) -> str:
    """Return the last assistant reply text (max 120 chars) from messages."""
    for m in reversed(msgs):
        if m["role"] == "assistant":
            return (m["content"] or "")[:120].replace("\n", " ")
    return ""
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
