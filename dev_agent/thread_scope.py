"""Resolution of the ``search_in_threads`` targeting arguments.

The targeting rules are pure: an explicit thread wins, then the current
dialog, then the orchestrator scope, then everything. Keeping them out of the
tool method lets each rule be read (and tested) on its own instead of as a
chain of nested branches.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

#: Scopes the search service understands.
KNOWN_SCOPES = ("thread", "current", "orchestrator", "all")

SCOPE_ERROR = "scope must be one of: current, orchestrator, all."
THREAD_SCOPE_ERROR = "scope='thread' requires 'thread_id'."
NO_ACTIVE_THREAD_ERROR = ("No active dialog thread: pass thread_id "
                          "or scope='all' explicitly.")


def normalize_scope(scope: Any, thread_id: Any,
                    orchestrator_slugs: Sequence[str]) -> Tuple[str, str]:
    """Return ``(scope, error)``; *scope* is one of KNOWN_SCOPES on success.

    An empty scope (or ``auto``) is resolved from what the caller supplied: a
    thread id means ``thread``, a named orchestrator means ``orchestrator``,
    and nothing at all means the current dialog.
    """
    value = str(scope or "").strip().lower()
    if value in KNOWN_SCOPES:
        return value, ""
    if value not in ("", "auto"):
        return "", SCOPE_ERROR
    if str(thread_id or "").strip():
        return "thread", ""
    if orchestrator_slugs:
        return "orchestrator", ""
    return "current", ""


def select_targets(scope: str, thread_id: Any, active_thread_id: str,
                   orchestrator_slugs: Sequence[str],
                   active_slug: str) -> Tuple[List[str], List[str], str]:
    """Return ``(thread_ids, slugs, error)`` for the resolved *scope*.

    ``thread_ids`` is None when the search is not narrowed to threads (the
    orchestrator and ``all`` scopes rely on *slugs* or on nothing at all).
    An empty *orchestrator_slugs* under the orchestrator scope falls back to
    the active orchestrator, so the caller never has to pre-fill it.
    """
    slugs = list(orchestrator_slugs or [])
    if str(thread_id or "").strip():
        return [str(thread_id).strip()], slugs, ""
    if scope == "current":
        if not active_thread_id:
            return None, slugs, NO_ACTIVE_THREAD_ERROR
        return [active_thread_id], slugs, ""
    if scope == "thread":
        return None, slugs, THREAD_SCOPE_ERROR
    if scope == "orchestrator" and not slugs:
        return None, [active_slug], ""
    return None, slugs, ""


def apply_active_thread(payload: Dict[str, Any], active_thread_id: str) -> Dict[str, Any]:
    """Attach the current thread id to a successful search payload."""
    if isinstance(payload, dict) and payload.get("ok"):
        payload["active_thread_id"] = active_thread_id or ""
    return payload


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
