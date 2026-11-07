# -*- coding: utf-8 -*-
"""core.chat_turn - one chat turn of an assistant dialog, decided outside the page.

The chat page used to call the provider layer and the thread store itself, so
the rules of a turn lived inside a render function: what history reaches the
model, when a thread is created, which messages are appended with what
attributes. That made the rules testable only by driving Streamlit, and a
second caller would have had to copy them.

This module owns the turn. The page owns the screen: it collects the input and
renders the spinner, the errors and the token line.
"""
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.api_layer import send_request
from core.threads import (
    append_thread_message,
    create_thread,
    load_thread_messages,
    messages_to_api_history,
    save_thread_file,
)

# ── the model call ───────────────────────────────────────────────────────────


def turn_history(thread_id: Optional[str]) -> List[Dict[str, str]]:
    """Return the API history of a thread; an empty list for a new dialog.

    The provider layer sees the stored exchange in its API form, so the turn
    never resends a message in a shape the transport cannot read.
    """
    if not thread_id:
        return []
    return messages_to_api_history(load_thread_messages(thread_id))


def run_chat_turn(user_message: str, assistant: Dict[str, Any],
                  file_context: str = "", history: Optional[List[Dict[str, str]]] = None,
                  lang: Optional[str] = None,
                  usage_callback: Optional[Callable] = None) -> str:
    """Run one chat turn through the provider layer and return the answer.

    Assistants with a native function tool run their own function-calling loop
    inside the provider layer, so a single call here covers both the plain and
    the tool-using assistant.

    Raises whatever the provider layer raises: the page decides how to show a
    failure, this module does not render.
    """
    return send_request(
        user_message, assistant, file_context,
        history=history or [], lang=lang,
        usage_callback=usage_callback,
    )


def collect_tokens() -> Tuple[Callable, Callable]:
    """Return ``(callback, read)`` collecting the usage figures of one turn.

    The callback is what the provider layer calls after a response; ``read``
    returns the collected figures, or ``None`` when the provider reported none
    at all - a thread without usage must stay distinguishable from one that
    reported zeros.
    """
    collected: Dict[str, int] = {}

    def _callback(tokens: Dict[str, Any]) -> None:
        collected.clear()
        collected.update({
            "in": tokens.get("in", 0),
            "out": tokens.get("out", 0),
            "cache": tokens.get("cache", 0) or 0,
        })

    def _read() -> Optional[Dict[str, int]]:
        return dict(collected) if collected else None

    return _callback, _read


# ── the stored exchange ──────────────────────────────────────────────────────


def persist_chat_turn(active_tid: Optional[str], assistant_id: str, assistant_name: str,
                      user_message: str, answer: str, *,
                      file_name: str = "", file_context: str = "",
                      tokens: Optional[Dict[str, int]] = None) -> str:
    """Create the thread when needed, append the exchange, return the thread id.

    Order matters and is kept from the page: the thread exists before the file
    is stored (a file row needs its thread), and the user message is written
    before the answer so a reader never sees a reply without its request. The
    file is attached only when both its name and its text are present - an
    empty upload is not a file.
    """
    tid = active_tid
    if not tid:
        tid = create_thread(assistant_id, assistant_name)
    if file_context and file_name:
        save_thread_file(tid, file_name, file_context)
    append_thread_message(tid, "user", user_message,
                          file_name=file_name, file_chars=len(file_context))
    append_thread_message(tid, "assistant", answer, tokens=tokens)
    return tid


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
