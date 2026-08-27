# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_settings - dedicated settings page for orchestrators.

Navigated to via orchestrator_settings:{slug}. Renders the same settings
as the old Settings tab but on its own page with a "Back to chat" button.
The settings sections are organised with native st.tabs so the active tab
survives reruns without manual button state.
"""
import streamlit as st
from core.i18n import t
from core.orchestrators import get_orchestrator, DEVAGENT_SLUG

# Import rendering helpers from the main orchestrator module
from ui.pages.orchestrator import (
    _render_models_settings,
    _render_prompt_settings,
    _render_economy_settings,
    _render_orchestrator_functions_settings,
    _render_orchestrator_instructions_settings,
    _render_orch_skills_settings,
    _render_orch_rag_bases,
    _render_orch_connections,
)


def page_orchestrator_settings(slug: str) -> None:
    """Render the settings page for the given orchestrator slug."""
    lang = st.session_state.get("ui_lang")
    orch = get_orchestrator(slug)
    if orch is None:
        st.error(t("orch_not_found", lang=lang))
        return

    orch_name = orch.get("name", slug)
    icon = "\U0001f6e0\ufe0f" if slug == DEVAGENT_SLUG else "\U0001f916"
    st.title(f"{icon} {orch_name} - {t('orch_tab_settings', lang=lang)}")

    # Back to chat button
    _, right_col = st.columns([4, 1])
    with right_col:
        if st.button("← " + t("orch_tab_chat", lang=lang),
                     key=f"orch_settings_back_{slug}",
                     use_container_width=True,
                     type="secondary"):
            st.session_state["current_page"] = f"orchestrator:{slug}"
            st.rerun()

    st.markdown("---")

    # Native tabs keep the selected settings section alive across reruns
    # (saves, toggles, ...) without any manual button/session-state plumbing.
    sub_tabs = st.tabs([
        t("orch_tab_models", lang=lang),
        t("orch_tab_prompt", lang=lang),
        t("orch_tab_economy", lang=lang),
        t("orch_tab_functions", lang=lang),
        t("orch_tab_orch_instructions", lang=lang),
        t("orch_tab_skills", lang=lang),
        t("orch_tab_rag_bases", lang=lang),
        t("orch_tab_connections", lang=lang),
    ])

    with sub_tabs[0]:
        _render_models_settings(slug, lang)
    with sub_tabs[1]:
        _render_prompt_settings(slug, lang)
    with sub_tabs[2]:
        _render_economy_settings(slug, lang)
    with sub_tabs[3]:
        _render_orchestrator_functions_settings(slug, lang)
    with sub_tabs[4]:
        _render_orchestrator_instructions_settings(slug, lang)
    with sub_tabs[5]:
        _render_orch_skills_settings(slug, lang)
    with sub_tabs[6]:
        _render_orch_rag_bases(slug, lang)
    with sub_tabs[7]:
        _render_orch_connections(slug, lang)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
