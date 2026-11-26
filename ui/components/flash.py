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
so a message queued WITHOUT an anchor appears below the content of the page
the user lands on instead of above its title.

A settings page has many save buttons, and a confirmation belongs under the
button that was pressed, not at the bottom of the page where it is easy to
miss and easy to mistake for another section's result. A save button
therefore records itself as the current ANCHOR (:func:`set_flash_anchor`),
the messages queued by its handler carry that anchor, and the page paints
them with :func:`render_flash_after` right after the widget. A message whose
button never rendered falls back to :func:`render_flash`, so it is never
lost.

Calls that are NOT followed by a rerun keep using plain ``st.success()`` -
those are painted by the current pass anyway.
"""
from __future__ import annotations

#: session_state key holding the queue of pending notifications.
FLASH_KEY = "_flash_messages"

#: session_state key holding the widget key of the button being handled.
ANCHOR_KEY = "_flash_anchor"

#: Upper bound on queued messages, so a failing loop cannot grow the queue.
MAX_PENDING = 10

#: Supported notification kinds.
_KINDS = ("success", "error", "warning", "info")


def _st():
    """Return the streamlit module installed right now.

    Resolved on every call rather than bound at import time: the page tree
    may be reloaded with a different module object, and a message must reach
    the same one the page renders into.
    """
    import streamlit

    return streamlit


def _queue() -> list:
    """Return the mutable pending-message list, creating it when absent."""
    st = _st()
    msgs = st.session_state.get(FLASH_KEY)
    if not isinstance(msgs, list):
        msgs = []
        st.session_state[FLASH_KEY] = msgs
    return msgs


def set_flash_anchor(anchor: str) -> None:
    """Record *anchor* as the button whose handler is running.

    A save button calls this when it is pressed, so the messages queued by
    its handler are painted under that button and nowhere else.
    """
    _st().session_state[ANCHOR_KEY] = str(anchor or "")


def current_flash_anchor() -> str:
    """Return the anchor recorded by the running button ("" when none)."""
    value = _st().session_state.get(ANCHOR_KEY)
    return value if isinstance(value, str) else ""


def clear_flash_anchor(anchor: str = "") -> None:
    """Forget the recorded anchor, or only when it equals *anchor*."""
    st = _st()
    if anchor and current_flash_anchor() != str(anchor):
        return
    st.session_state[ANCHOR_KEY] = ""


def flash(text: str, kind: str = "success", anchor=None) -> None:
    """Queue *text* for the next render pass.

    Args:
        text: already-translated message; empty text is ignored.
        kind: one of "success", "error", "warning", "info"; an unknown
            value falls back to "success".
        anchor: widget key of the button the message belongs to. ``None``
            (the default) uses the anchor of the button being handled - a
            plain call inside a save handler lands under that button. An
            explicit empty string means "the page in general" and is painted
            by :func:`render_flash` at the end of the page.
    """
    if not text:
        return
    if kind not in _KINDS:
        kind = "success"
    resolved = current_flash_anchor() if anchor is None else str(anchor or "")
    msgs = _queue()
    msgs.append({"kind": kind, "text": str(text), "anchor": resolved})
    del msgs[:-MAX_PENDING]


def flash_success(text: str, anchor=None) -> None:
    """Queue a green confirmation (the usual "saved" message)."""
    flash(text, "success", anchor)


def flash_error(text: str, anchor=None) -> None:
    """Queue a red failure message."""
    flash(text, "error", anchor)


def flash_warning(text: str, anchor=None) -> None:
    """Queue a yellow warning message."""
    flash(text, "warning", anchor)


def flash_info(text: str, anchor=None) -> None:
    """Queue a blue informational message."""
    flash(text, "info", anchor)


def _painters() -> dict:
    """Return the painter of each supported kind."""
    st = _st()
    return {
        "success": st.success,
        "error": st.error,
        "warning": st.warning,
        "info": st.info,
    }


def render_flash_after(anchor: str) -> None:
    """Paint the queued messages tied to *anchor* and drop them.

    Call it right after the widget named by *anchor*: the confirmation then
    appears under the button that produced it. Messages of other anchors are
    left in the queue for their own widget or for the page-level fallback.
    """
    st = _st()
    anchor = str(anchor or "")
    if not anchor:
        return
    msgs = st.session_state.get(FLASH_KEY)
    if not isinstance(msgs, list):
        msgs = []
    painters = _painters()
    keep = []
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("anchor") or "") != anchor:
            keep.append(msg)
            continue
        kind = msg.get("kind") if msg.get("kind") in painters else "success"
        text = msg.get("text") or ""
        if text:
            painters[kind](text)
    st.session_state[FLASH_KEY] = keep
    # The button has rendered its own messages: the anchor must not leak to
    # a handler of another button that queues without setting one.
    clear_flash_anchor(anchor)


def render_flash() -> None:
    """Paint every queued notification and empty the queue.

    Safe to call on every render pass: it is a no-op when nothing is queued.
    This is the page-level fallback: a message whose own button never
    rendered still reaches the user here instead of staying in the queue.
    """
    st = _st()
    msgs = st.session_state.get(FLASH_KEY)
    if not isinstance(msgs, list) or not msgs:
        if msgs is not None:
            st.session_state[FLASH_KEY] = []
        return
    st.session_state[FLASH_KEY] = []
    painters = _painters()
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        kind = msg.get("kind") if msg.get("kind") in painters else "success"
        text = msg.get("text") or ""
        if text:
            painters[kind](text)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
