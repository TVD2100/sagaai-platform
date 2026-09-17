# -*- coding: utf-8 -*-
"""
core.orchestrator_nav - sidebar ordering helpers for the employee list.

Mirrors ``core.assistant_nav`` for orchestrators ("employees" in the UI):
the sidebar shows a fixed-size visible block (by default 5 orchestrators)
followed by a collapsed list with a search field, and the order is stable
across app restarts because it is computed from persistent data.

Each orchestrator has ONE effective sort time:

  * orchestrators with dialogues use the newest dialogue time (the maximum
    ``updated_at`` among the orchestrator's threads in the DevAgent
    database, where the orchestrator slug is stored in ``assistant_id``);
  * orchestrators without dialogues use ``created_at`` (falling back to
    ``updated_at``), so a freshly created orchestrator lands at the very
    top;
  * orchestrators missing both timestamps go last, ordered by name.

This is a pure module (no streamlit import), so the ordering rules are
unit-testable in isolation and reusable by the UI layer.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.assistant_nav import _num, _parse_ts, last_dialogue_at

DEFAULT_VISIBLE_ORCHESTRATORS = 5


def last_used_at(slug: Optional[str],
                 threads: Optional[List[Dict[str, Any]]]) -> Optional[datetime]:
    """Return the newest dialogue time among threads of *slug*, or None.

    *threads* is a list of DevAgent thread metadata dicts (as returned by
    ``core.threads_devagent.list_devagent_threads``); the orchestrator slug
    is stored in the ``assistant_id`` field.
    """
    return last_dialogue_at(slug, threads)


def sort_orchestrators(orchestrators: List[Dict[str, Any]],
                       threads: Optional[List[Dict[str, Any]]] = None
                       ) -> List[Dict[str, Any]]:
    """Return *orchestrators* sorted for the sidebar (newest activity first).

    An orchestrator's activity time is its latest dialogue time when it has
    dialogues, otherwise its ``created_at`` (falling back to ``updated_at``).
    Ties are broken by a stable name sort. The input list is not modified.
    """
    def key(o: Dict[str, Any]) -> Tuple[float, str]:
        last = last_dialogue_at(o.get("slug"), threads)
        created = _parse_ts(o.get("created_at")) or _parse_ts(o.get("updated_at"))
        # Effective activity time: last dialogue when present, otherwise the
        # creation time. Negative timestamps sort newest-first; undated
        # orchestrators get +1.0 so they sink below every dated one.
        sort_time = last if last is not None else created
        return (
            -_num(sort_time) if sort_time is not None else 1.0,
            (o.get("name") or "").lower(),
        )

    return sorted(orchestrators, key=key)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
