"""
core.recent_assistants - track recently used assistant IDs in session_state.
"""
import streamlit as st

_MAX_RECENT = 5


def record_assistant_use(assistant_id: str) -> None:
    """Move assistant_id to front of recent_assistant_ids list, limit to 5.

    Also mirrors the value into the legacy ``recent_skill_ids`` session key
    so older UI code (which still reads it) keeps working during the
    transition period.
    """
    recent = list(st.session_state.get("recent_assistant_ids", []))
    recent = [sid for sid in recent if sid != assistant_id]
    recent.insert(0, assistant_id)
    st.session_state["recent_assistant_ids"] = recent[:_MAX_RECENT]
    # Legacy mirror for backward compatibility.
    st.session_state["recent_skill_ids"] = list(st.session_state["recent_assistant_ids"])
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
