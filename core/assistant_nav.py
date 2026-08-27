# -*- coding: utf-8 -*-
"""
core.assistant_nav - sidebar ordering helpers for the assistant list.

The sidebar shows a fixed-size visible block (by default 5 assistants).
The order is stable across app restarts because it is computed from the
persistent data instead of the session-only "recently used" list.

Each assistant has ONE effective sort time:

  * assistants with dialogues use the newest dialogue time (the maximum
    ``updated_at`` among all of the assistant's chat threads);
  * assistants without dialogues use ``created_at``
    (falling back to ``updated_at``), so a freshly created assistant -
    whose creation time is "now" - lands at the very top;
  * assistants missing both timestamps go last, ordered by name.

This is a pure module (no streamlit import), so the ordering rules are
unit-testable in isolation and reusable by the UI layer.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_VISIBLE_ASSISTANTS = 5


def _parse_ts(value: Any) -> Optional[datetime]:
    """Return a naive datetime for *value*, or None when unusable.

    Accepts ``datetime`` objects, numeric unix timestamps and ISO strings
    (the app stores ``datetime.now().isoformat()``). Invalid values are
    ignored so a malformed row never crashes the sidebar.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OSError, OverflowError, ValueError):
            return None
    try:
        return datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _num(dt: Optional[datetime]) -> float:
    """Return a comparable numeric form of *dt* (0 for None)."""
    return dt.timestamp() if dt is not None else 0.0


def last_dialogue_at(assistant_id: Optional[str],
                     threads: Optional[List[Dict[str, Any]]]) -> Optional[datetime]:
    """Return the newest dialogue time among chat threads of *assistant_id*.

    Uses ``threads`` (a list of thread metadata dicts, as returned by
    ``core.threads.list_chat_threads``). Falls back to ``created_at`` when
    ``updated_at`` is missing. Returns None when the assistant has no
    dialogues.
    """
    if not assistant_id or not threads:
        return None
    best: Optional[datetime] = None
    for th in threads:
        if th.get("assistant_id") != assistant_id:
            continue
        ts = _parse_ts(th.get("updated_at")) or _parse_ts(th.get("created_at"))
        if ts is not None and (best is None or ts > best):
            best = ts
    return best


def sort_assistants(assistants: List[Dict[str, Any]],
                    threads: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Return *assistants* sorted for the sidebar (newest activity first).

    Each assistant's activity time is its latest dialogue time when it has
    dialogues, otherwise its ``created_at`` (falling back to ``updated_at``).
    Ties are broken by a stable name sort. The input list is not modified.
    """
    def key(a: Dict[str, Any]) -> Tuple[float, str]:
        last = last_dialogue_at(a.get("id"), threads)
        created = _parse_ts(a.get("created_at")) or _parse_ts(a.get("updated_at"))
        # Effective activity time: last dialogue when present, otherwise the
        # creation time. Negative timestamps sort newest-first; undated
        # assistants get +1.0 so they sink below every dated assistant.
        sort_time = last if last is not None else created
        return (
            -_num(sort_time) if sort_time is not None else 1.0,
            (a.get("name") or "").lower(),
        )

    return sorted(assistants, key=key)


def split_nav_lists(assistants: List[Any],
                    visible_count: Optional[int] = None) -> Tuple[List[Any], List[Any]]:
    """Split an ordered assistant list into (visible, remaining).

    ``visible`` contains at most ``visible_count`` entries;
    ``remaining`` contains the rest. Defaults to
    ``DEFAULT_VISIBLE_ASSISTANTS`` (5).
    """
    count = DEFAULT_VISIBLE_ASSISTANTS if visible_count is None else visible_count
    if count < 0:
        count = 0
    return assistants[:count], assistants[count:]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
