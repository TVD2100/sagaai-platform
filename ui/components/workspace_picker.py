"""
ui.components.workspace_picker — reusable workspace / file picker component.

Provides ``render_workspace_picker()`` for any Streamlit page to let the user
select a work folder OR a single target file via an absolute path.

The component uses a button-first pattern:
- a prominent button "📁 Specify folder/file path"
- clicking the button reveals a text input for the path
- the path is validated: folder → normal workspace; file → single-file mode

All user-facing strings go through t(key, lang=lang).
"""

from __future__ import annotations

import os
from typing import Callable, Optional

import streamlit as st

from core.i18n import t


# ── session-state keys ──────────────────────────────────────────────────────
def _picker_state_keys(prefix: str) -> dict:
    """Return session-state key names for a namespaced picker."""
    return {
        "ws":              f"{prefix}_workspace",
        "ws_assess":       f"{prefix}_workspace_assess",
        "target_file":     f"{prefix}_target_file",   # absolute path when single-file mode
        "show_input":      f"{prefix}_show_path_input",
    }


def _init_picker_state(prefix: str) -> None:
    """Ensure session-state keys exist."""
    keys = _picker_state_keys(prefix)
    defaults = {
        keys["ws"]:          "",
        keys["ws_assess"]:   None,
        keys["target_file"]: "",
        keys["show_input"]:  False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _resolve_path(raw: str) -> tuple[str, bool, bool]:
    """
    Validate and resolve a user-supplied path.

    Returns (resolved_abs_path, is_dir, is_file).
    If neither a file nor a directory, is_dir and is_file are both False.
    """
    expanded = os.path.expanduser(raw.strip())
    abs_path = os.path.abspath(expanded)
    if os.path.isdir(abs_path):
        return abs_path, True, False
    if os.path.isfile(abs_path):
        return abs_path, False, True
    return abs_path, False, False


def render_workspace_picker(
    prefix: str = "picker",
    lang: Optional[str] = None,
    *,
    on_workspace_set: Optional[Callable[[str], None]] = None,
    on_file_set: Optional[Callable[[str], None]] = None,
) -> None:
    """
    Render a compact workspace / file picker UI.

    Parameters
    ----------
    prefix : str
        Unique prefix to separate session-state keys across different pages.
    lang : Optional[str]
        UI language code (e.g. "ru", "en").
    on_workspace_set : Optional[Callable[[str], None]]
        Called with the selected folder path every time a valid workspace
        is set (new text input commit).
    on_file_set : Optional[Callable[[str], None]]
        Called with the absolute file path when single-file mode is activated.
    """
    _init_picker_state(prefix)
    keys = _picker_state_keys(prefix)

    ws = st.session_state.get(keys["ws"]) or ""
    target_file = st.session_state.get(keys["target_file"]) or ""
    show_input = st.session_state.get(keys["show_input"], False)

    current_target = target_file or ws

    # ── Button: show / hide path input ────────────────────────────────────
    btn_label = (
        t("devagent_hide_path_input", lang=lang) if show_input
        else t("devagent_path_btn", lang=lang)
    )
    if st.button(btn_label, key=f"{prefix}_toggle_path_btn", type="secondary",
                 use_container_width=True):
        st.session_state[keys["show_input"]] = not show_input
        st.rerun()

    # ── Active path badge ─────────────────────────────────────────────────
    if current_target:
        mode_info = ""
        if target_file:
            mode_info = f"({t('devagent_single_file_mode', lang=lang, path=current_target)})"
        st.caption(f"📍 {current_target} {mode_info}")

    # ── Path input (revealed by button) ───────────────────────────────────
    if show_input:
        st.caption(t("devagent_path_input_help", lang=lang))
        col_a, col_b = st.columns([4, 1])
        with col_a:
            path_input = st.text_input(
                t("devagent_path_input_label", lang=lang),
                placeholder=t("devagent_path_input_ph", lang=lang),
                key=f"{prefix}_path_input",
                label_visibility="visible",
            )
        with col_b:
            st.write("")
            st.write("")
            if st.button("✔️ OK", key=f"{prefix}_path_ok", use_container_width=True):
                path_input = st.session_state.get(f"{prefix}_path_input", "")
                if path_input and path_input.strip():
                    resolved, is_dir, is_file = _resolve_path(path_input.strip())
                    if is_dir:
                        # Folder mode
                        st.session_state[keys["ws"]] = resolved
                        st.session_state[keys["target_file"]] = ""
                        if on_workspace_set:
                            on_workspace_set(resolved)
                        st.success(t("devagent_path_set", lang=lang, path=resolved))
                        st.rerun()
                    elif is_file:
                        # Single-file mode: workspace = parent dir
                        parent_dir = os.path.dirname(resolved)
                        st.session_state[keys["ws"]] = parent_dir
                        st.session_state[keys["target_file"]] = resolved
                        if on_workspace_set:
                            on_workspace_set(parent_dir)
                        if on_file_set:
                            on_file_set(resolved)
                        st.success(t("devagent_single_file_mode", lang=lang, path=resolved))
                        st.rerun()
                    else:
                        st.error(t("devagent_path_bad_path", lang=lang, path=resolved))
                st.rerun()

    # ── Empty-state: show welcome message in chat area, not here ──────────
    # (The welcome is rendered by the page itself as the first assistant message.)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
