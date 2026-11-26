# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_settings - dedicated settings page for orchestrators.

Navigated to via orchestrator_settings:{slug}. Renders the settings of one
orchestrator on its own page with a "Back to chat" button.

The sections are native st.tabs, created STATEFUL (``on_change="rerun"``).
Without ``on_change="rerun"`` the widget keeps no state at all: its key is
only a CSS identity, so a rerun falls back to the first tab and a pinned
section is silently ignored. With it, the active label lives in
``st.session_state[key]``, and a save handler pins the section it saved by
writing that slot before the container is created - after its st.rerun() the
page reopens on the tab the user was editing and the queued confirmation is
visible next to the section it belongs to.

The container is created with the SAME arguments on every pass. Streamlit
derives a widget identity from its parameters, so passing ``default`` on the
pinning pass only would describe a different widget on the next pass and
discard the stored label: any click inside the section would throw the user
back to the first tab.
"""
import streamlit as st

from core.i18n import t
from core.orchestrators import DEVAGENT_SLUG, get_orchestrator

# Import rendering helpers from the main orchestrator module
from ui.pages.orchestrator import (
    SETTINGS_SECTIONS,
    _render_delegation_settings,
    _render_economy_settings,
    _render_models_settings,
    _render_orch_connections,
    _render_orch_memory,
    _render_orch_rag_bases,
    _render_orch_skills_settings,
    _render_orchestrator_functions_settings,
    _render_orchestrator_instructions_settings,
    _render_other_settings,
    _render_prompt_settings,
    settings_section_key,
    settings_tabs_key,
)
from ui.pages.orchestrator_sandbox import render_sandbox_settings


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
        t("orch_tab_memory", lang=lang),
        t("orch_tab_delegation", lang=lang),
        t("orch_tab_security", lang=lang),
        t("orch_tab_other", lang=lang),
    ]

    # A save handler asks for its own section before calling st.rerun(); the
    # request is consumed here, BEFORE the tabs are created, so the active tab
    # survives the rerun and the user stays where the save happened.
    tabs_key = settings_tabs_key(slug)
    requested = st.session_state.pop(settings_section_key(slug), None)
    if requested in SETTINGS_SECTIONS:
        # A requested section wins over whatever tab was active. The label is
        # written into the widget slot here, BEFORE the container is created:
        # that is the only moment Streamlit lets a widget state change, and
        # the stateful tabs read their active label from session_state.
        st.session_state[tabs_key] = labels[SETTINGS_SECTIONS.index(requested)]
    elif st.session_state.get(tabs_key) not in labels:
        # The stored selection is a translated label, so switching the UI
        # language leaves a value that matches no tab; drop it instead of
        # handing the widget an unknown tab name.
        st.session_state.pop(tabs_key, None)

    # key= pins the container identity and on_change="rerun" is what makes
    # the widget stateful: the active label is stored in session_state and a
    # rerun reopens the tab the user was working in.
    #
    # The call passes the SAME arguments on every render pass. Streamlit
    # derives a widget identity from its parameters: handing ``default`` over
    # on the pinning pass only would describe a different widget, and the
    # active label stored for the previous one would be discarded - the user
    # would be thrown onto the first tab by the next click anywhere in the
    # section they are editing.
    sub_tabs = st.tabs(labels, key=tabs_key, on_change="rerun")

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
        _render_orch_memory(slug, lang)
    with sub_tabs[9]:
        _render_delegation_settings(slug, lang)
    with sub_tabs[10]:
        # The access panel owns its own tab: it answers how generated code is
        # isolated on this machine, which is a question of its own and not a
        # part of the miscellaneous settings.
        render_sandbox_settings(slug, lang)
    with sub_tabs[11]:
        _render_other_settings(slug, lang)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
