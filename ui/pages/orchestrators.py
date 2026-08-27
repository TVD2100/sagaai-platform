# -*- coding: utf-8 -*-
"""
ui.pages.orchestrators - Orchestrators management page.

Shows all orchestrators (custom and built-in DevAgent), with actions:
  - open an orchestrator (navigate to its page),
  - delete a CUSTOM orchestrator ONLY after an inline confirmation
    (first click asks "Deleting <name>. Continue?", second click deletes),
  - create a NEW orchestrator via DevAgent (sends a task to the DevAgent page).

This page is reachable from the sidebar section "Orchestrators".
"""
import streamlit as st

from core.i18n import t
from core.orchestrators import (
    list_orchestrators,
    delete_orchestrator,
    DEVAGENT_SLUG,
)


def _go_to_page(page_id: str) -> None:
    st.session_state["current_page"] = page_id
    st.rerun()


def page_orchestrators() -> None:
    """Render the Orchestrators management page."""
    lang = st.session_state.get("ui_lang")

    st.title(t("orch_mgmt_title", lang=lang))
    st.caption(t("orch_mgmt_intro", lang=lang))

    # ── Create via DevAgent ────────────────────────────────────────────────
    st.markdown("---")
    st.subheader(t("orch_mgmt_create_title", lang=lang))
    st.markdown(t("orch_mgmt_create_desc", lang=lang))
    if st.button(t("orch_mgmt_create_btn", lang=lang), key="orch_mgmt_create_btn",
                 type="primary", use_container_width=True):
        _go_to_page(f"orchestrator:{DEVAGENT_SLUG}")

    # ── List of orchestrators ──────────────────────────────────────────────
    st.markdown("---")
    st.subheader(t("orch_mgmt_list_title", lang=lang))
    orch_list = list_orchestrators()
    if not orch_list:
        st.info(t("orch_mgmt_empty", lang=lang))
        return

    for orch in orch_list:
        slug = orch.get("slug", "")
        name = orch.get("name", slug)
        description = orch.get("description", "")
        is_builtin = orch.get("is_builtin", False)
        tools = orch.get("tools", [])
        sort_order = orch.get("sort_order", 100)

        with st.container(border=True):
            badge = "🛠️" if is_builtin else "🤖🤖"
            st.markdown(f"**{badge} {name}**" + ("  `builtin`" if is_builtin else ""))
            if description:
                st.caption(f"📝 {description}")
            st.caption(f"🔑 `{slug}` · 🛠 {len(tools)} tools · sort {sort_order}")

            col_open, col_settings, col_del = st.columns([2, 2, 1])
            with col_open:
                if st.button(t("orch_mgmt_open_btn", lang=lang),
                             key=f"orch_mgmt_open_{slug}",
                             use_container_width=True,
                             type="primary"):
                    _go_to_page(f"orchestrator:{slug}")
            with col_settings:
                if st.button("⚙️ " + t("orch_tab_settings", lang=lang),
                             key=f"orch_mgmt_settings_{slug}",
                             use_container_width=True,
                             type="secondary"):
                    _go_to_page(f"orchestrator_settings:{slug}")
            with col_del:
                if not is_builtin:
                    if st.button("🗑", key=f"orch_mgmt_del_{slug}",
                                 help=t("orch_mgmt_delete_help", lang=lang),
                                 use_container_width=True):
                        st.session_state["orch_mgmt_confirm_delete"] = slug
                        st.rerun()

        # ── Inline delete confirmation (destructive action gate) ───────────
        if (not is_builtin
                and st.session_state.get("orch_mgmt_confirm_delete") == slug):
            with st.container(border=True):
                st.warning(t("confirm_delete", lang=lang, name=name))
                c_yes, c_no = st.columns(2)
                with c_yes:
                    if st.button(t("btn_yes_delete", lang=lang),
                                 key=f"orch_mgmt_del_yes_{slug}",
                                 type="primary", use_container_width=True):
                        ok = delete_orchestrator(slug)
                        st.session_state["orch_mgmt_confirm_delete"] = None
                        if ok:
                            st.success(t("orch_mgmt_deleted", lang=lang, name=name))
                        else:
                            st.error(t("orch_mgmt_delete_error", lang=lang, name=name))
                        st.rerun()
                with c_no:
                    if st.button(t("btn_cancel", lang=lang),
                                 key=f"orch_mgmt_del_no_{slug}",
                                 use_container_width=True):
                        st.session_state["orch_mgmt_confirm_delete"] = None
                        st.rerun()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
