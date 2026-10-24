# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_settings - dedicated settings page for orchestrators.

Navigated to via orchestrator_settings:{slug}. Renders the settings of one
orchestrator on its own page with a "Back to chat" button.

The sections are native st.tabs. Their selection is kept across reruns by a
stable tab key; a save handler additionally pins the section it saved, so
after its st.rerun() the page reopens on the tab the user was editing and
the queued confirmation is visible next to the section it belongs to.
"""
import streamlit as st
from core.i18n import t
from core.orchestrators import get_orchestrator, DEVAGENT_SLUG

# Import rendering helpers from the main orchestrator module
from ui.pages.orchestrator import (
    SETTINGS_SECTIONS,
    settings_section_key,
    settings_tabs_key,
    _render_models_settings,
    _render_prompt_settings,
    _render_economy_settings,
    _render_orchestrator_functions_settings,
    _render_orchestrator_instructions_settings,
    _render_orch_skills_settings,
    _render_orch_rag_bases,
    _render_orch_connections,
    _render_delegation_settings,
    _render_other_settings,
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

    # Sections are rendered as tabs. The selection is kept across reruns by
    # the tab key plus the section pin set by a save handler, so a save never
    # throws the user back to the first tab.
    labels = [
        t("orch_tab_models", lang=lang),
        t("orch_tab_prompt", lang=lang),
        t("orch_tab_economy", lang=lang),
        t("orch_tab_functions", lang=lang),
        t("orch_tab_orch_instructions", lang=lang),
        t("orch_tab_skills", lang=lang),
        t("orch_tab_rag_bases", lang=lang),
        t("orch_tab_connections", lang=lang),
        t("orch_tab_delegation", lang=lang),
        t("orch_tab_other", lang=lang),
    ]

    # A save handler asks for its own section before calling st.rerun(); the
    # request is consumed here, BEFORE the tabs are created, so the active tab
    # survives the rerun and the user stays where the save happened.
    tabs_key = settings_tabs_key(slug)
    requested = st.session_state.pop(settings_section_key(slug), None)
    if requested in SETTINGS_SECTIONS:
        st.session_state[tabs_key] = labels[SETTINGS_SECTIONS.index(requested)]
    elif st.session_state.get(tabs_key) not in labels:
        # The stored selection is a translated label, so switching the UI
        # language leaves a value that matches no tab; drop it instead of
        # handing the widget an unknown tab name.
        st.session_state.pop(tabs_key, None)

    # key= pins the container identity, so the selected tab is preserved
    # across reruns and a settings save no longer falls back to the first tab.
    sub_tabs = st.tabs(labels, key=tabs_key)

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
    with sub_tabs[8]:
        _render_delegation_settings(slug, lang)
    with sub_tabs[9]:
        _render_other_settings(slug, lang)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
