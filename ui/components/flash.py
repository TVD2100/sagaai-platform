# -*- coding: utf-8 -*-
"""
ui.components.flash - one-shot notifications that survive a Streamlit rerun.

A message rendered with ``st.success()`` (or st.error/st.warning/st.info)
immediately before ``st.rerun()`` is lost: the rerun discards the whole render
pass, so the user never sees it. This module keeps such messages in
``st.session_state`` and paints them exactly once, on the next render pass.

Usage inside a page::

    from ui.components.flash import flash_success

    if st.button(...):
        save_something()
        flash_success(t("saved_ok", lang=lang))
        st.rerun()

``ui.app.main()`` calls :func:`render_flash` right after the page dispatch,
so a queued message appears below the content of the page the user lands on
(including a settings page) instead of above its title. Calls that are NOT
followed by a rerun keep using plain ``st.success()`` - those are painted by
the current pass anyway.
"""
from __future__ import annotations

import streamlit as st

#: session_state key holding the queue of pending notifications.
FLASH_KEY = "_flash_messages"

#: Upper bound on queued messages, so a failing loop cannot grow the queue.
MAX_PENDING = 10

#: Supported notification kinds and the Streamlit painter of each.
_KINDS = ("success", "error", "warning", "info")


def _queue() -> list:
    """Return the mutable pending-message list, creating it when absent."""
    msgs = st.session_state.get(FLASH_KEY)
    if not isinstance(msgs, list):
        msgs = []
        st.session_state[FLASH_KEY] = msgs
    return msgs


def flash(text: str, kind: str = "success") -> None:
    """Queue *text* for the next render pass.

    Args:
        text: already-translated message; empty text is ignored.
        kind: one of "success", "error", "warning", "info"; an unknown
            value falls back to "success".
    """
    if not text:
        return
    if kind not in _KINDS:
        kind = "success"
    msgs = _queue()
    msgs.append({"kind": kind, "text": str(text)})
    del msgs[:-MAX_PENDING]


def flash_success(text: str) -> None:
    """Queue a green confirmation (the usual "saved" message)."""
    flash(text, "success")


def flash_error(text: str) -> None:
    """Queue a red failure message."""
    flash(text, "error")


def flash_warning(text: str) -> None:
    """Queue a yellow warning message."""
    flash(text, "warning")


def flash_info(text: str) -> None:
    """Queue a blue informational message."""
    flash(text, "info")


def render_flash() -> None:
    """Paint the queued notifications and empty the queue.

    Safe to call on every render pass: it is a no-op when nothing is queued.
    A malformed queue value is replaced by an empty list instead of raising.
    """
    msgs = st.session_state.get(FLASH_KEY)
    if not isinstance(msgs, list) or not msgs:
        if msgs is not None:
            st.session_state[FLASH_KEY] = []
        return
    st.session_state[FLASH_KEY] = []
    painters = {
        "success": st.success,
        "error": st.error,
        "warning": st.warning,
        "info": st.info,
    }
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        kind = msg.get("kind") if msg.get("kind") in painters else "success"
        text = msg.get("text") or ""
        if text:
            painters[kind](text)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
