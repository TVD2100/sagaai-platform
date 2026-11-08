# -*- coding: utf-8 -*-
"""
ui.pages.assistants_nav - the navigation header of the assistants page.

The page is two flows (a list and an editing form) plus one navigation
control that is shared by both. The control only reads session state and
writes the page switch, so it lives here instead of in the page module,
which keeps the list and form helpers together.
"""
import streamlit as st

from core.i18n import t


def _render_back_to_chat(lang: str) -> None:
    """Render the "back to chat" button, remembering the active assistant.

    Returning keeps the assistant that was being worked on, so the chat opens
    on the same card instead of a blank selection. The legacy ``skill`` keys
    are read too, so a session restored from an older build still resolves
    the same card.
    """
    _, right_col = st.columns([4, 1])
    with right_col:
        if st.button("← " + t("orch_tab_chat", lang=lang),
                     key="assistant_back_chat",
                     use_container_width=True,
                     type="secondary"):
            aid = (
                st.session_state.get("edit_assistant_id")
                or st.session_state.get("edit_skill_id")
                or st.session_state.get("last_active_entity_id")
                or st.session_state.get("selected_assistant_id")
                or st.session_state.get("selected_skill_id")
            )
            if aid:
                st.session_state["last_active_entity_type"] = "assistant"
                st.session_state["last_active_entity_id"] = aid
                st.session_state["selected_assistant_id"] = aid
                st.session_state["selected_skill_id"] = aid
            st.session_state["current_page"] = "run"
            st.rerun()


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
