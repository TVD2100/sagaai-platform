# -*- coding: utf-8 -*-
"""core.orchestrator_step - writing one agent step's output into its thread.

The orchestrator page kept this rule inside a render module: which messages of
a finished step reach the dialog thread, what attributes travel with them and
how the saved cursor advances. As long as the rule lived there, a second
caller - a scheduled run, a resumed dialog - would have had to restate it, and
a drift between the two copies would silently duplicate or lose messages.

This module owns the write. The page owns the session state around it: it
decides when a step ran, this module decides what the thread receives.
"""
from typing import Any, Dict, List, Optional

from core.threads_devagent import append_thread_message


def messages_to_persist(history: List[Dict[str, Any]], saved_count: int) -> List[Dict[str, Any]]:
    """Return the messages of *history* that the thread does not have yet.

    ``saved_count`` is the cursor kept in session state. A cursor past the end
    of the history - the dialog was reset while a step was running - yields an
    empty list instead of an exception: the thread keeps what it has and the
    next step starts a fresh window.
    """
    if not history:
        return []
    if saved_count < 0:
        saved_count = 0
    if saved_count > len(history):
        return []
    return list(history[saved_count:])


def message_row(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Return the thread-store arguments of one history message.

    The history of a step carries both the text of a message and the data the
    feed renders (events, usage, the hidden flag). The store keeps them apart,
    so this is the single place that decides which key maps to which column.
    """
    return {
        "role": msg.get("role", "user"),
        "content": msg.get("content", ""),
        "file_name": msg.get("file_name", ""),
        "file_chars": msg.get("file_chars", 0),
        "events": msg.get("_events"),
        "tokens": msg.get("_tokens"),
        "hidden": bool(msg.get("hidden")),
    }


def persist_step_messages(thread_id: Optional[str], history: List[Dict[str, Any]],
                          saved_count: int) -> int:
    """Append the unsaved messages to the thread; return the new cursor.

    A dialog without a thread keeps its history in session state only: the
    first step of a brand-new dialog runs before the thread exists, and it must
    not be written to some other dialog's thread. The cursor moves even then,
    so the same messages are not reconsidered after the thread appears.
    """
    new_count = len(history or [])
    if not thread_id or not history:
        return new_count if history else saved_count
    for msg in messages_to_persist(history, saved_count):
        append_thread_message(thread_id, **message_row(msg))
    return new_count


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
