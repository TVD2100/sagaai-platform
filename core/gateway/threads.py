"""
core.gateway.threads - reading dialogs through the facade.

Every function is a thin translation of an existing storage call into a
transport type. No query is written here: ``core.threads_devagent`` already
owns how a message is stored (the ``messages`` table of ``devagent.db``),
how the tool-result prefix is packed into the content column and how the
index row is maintained. A second reader would drift from it within one
release.

The one addition is the scope: ``allowed_slugs`` narrows what a caller may
see, and a thread outside the scope answers ``thread_not_found`` - never
``forbidden``, because a 403 would confirm that the id exists.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.gateway import errors
from core.gateway.dto import (
    MessageView,
    SearchPage,
    ThreadPage,
    ThreadSummary,
    ThreadWindow,
)

log = logging.getLogger("core.gateway.threads")

#: Default page size of a thread listing.
DEFAULT_LIMIT = 50
#: Hard ceiling a caller cannot exceed by asking.
MAX_LIMIT = 200


# ─── Conversions ────────────────────────────────────────────────────────────


def _summary_of(row: Dict[str, Any]) -> ThreadSummary:
    """Build a ``ThreadSummary`` from an index row."""
    row = row if isinstance(row, dict) else {}
    count = row.get("message_count")
    try:
        count_value: Optional[int] = None if count is None else int(count)
    except (TypeError, ValueError):
        count_value = None
    return ThreadSummary(
        thread_id=str(row.get("thread_id") or row.get("id") or ""),
        title=str(row.get("title") or ""),
        agent_slug=str(row.get("orchestrator_slug")
                       or row.get("assistant_id") or ""),
        agent_name=str(row.get("orchestrator_name")
                       or row.get("assistant_name") or ""),
        workspace=str(row.get("workspace") or ""),
        target_file=str(row.get("target_file") or ""),
        kind=str(row.get("kind") or row.get("type") or ""),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
        message_count=count_value,
    )


def _message_of(index: int, row: Dict[str, Any]) -> MessageView:
    """Build a ``MessageView`` from a stored message row."""
    row = row if isinstance(row, dict) else {}
    events = row.get("events") or row.get("_events") or []
    tokens = row.get("tokens") or row.get("_tokens") or {}
    return MessageView(
        index=int(row.get("index", index) or index),
        role=str(row.get("role") or "user"),
        content=str(row.get("content") or ""),
        ts=str(row.get("ts") or ""),
        file_name=str(row.get("file_name") or ""),
        file_chars=int(row.get("file_chars") or 0),
        hidden=bool(row.get("hidden")),
        events=list(events) if isinstance(events, (list, tuple)) else [],
        tokens=dict(tokens) if isinstance(tokens, dict) else {},
    )


def _bounded_limit(limit: Any) -> int:
    """Clamp a requested page size into ``[1, MAX_LIMIT]``."""
    try:
        value = int(limit or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def _scope_filter(rows: List[Dict[str, Any]],
                  allowed_slugs: Optional[List[str]]) -> List[Dict[str, Any]]:
    """Return the rows a caller may see under *allowed_slugs*.

    ``None`` means "no scope" - the platform owner addressing its own data.
    An empty list means "nothing at all": the safe reading of a token with
    no slugs, and the reason a missing scope can never silently widen.
    """
    if allowed_slugs is None:
        return list(rows)
    allowed = {str(slug).strip() for slug in allowed_slugs if str(slug).strip()}
    if not allowed:
        return []
    if "*" in allowed:
        return list(rows)
    out: List[Dict[str, Any]] = []
    for row in rows:
        slug = str((row or {}).get("orchestrator_slug")
                   or (row or {}).get("assistant_id") or "")
        if slug in allowed:
            out.append(row)
    return out


# ─── Readers ────────────────────────────────────────────────────────────────


def list_threads(*, slugs: Optional[List[str]] = None,
                 date_from: str = "", date_to: str = "",
                 date_field: str = "updated",
                 limit: int = DEFAULT_LIMIT, offset: int = 0,
                 order: str = "desc",
                 allowed_slugs: Optional[List[str]] = None) -> ThreadPage:
    """Return one page of dialogs visible to the caller."""
    from core.threads_devagent import list_threads_filtered

    page_size = _bounded_limit(limit)
    try:
        result = list_threads_filtered(
            slugs=slugs,
            date_from=date_from or None,
            date_to=date_to or None,
            date_field=date_field or "updated",
            limit=page_size,
            offset=max(0, int(offset or 0)),
            order=order or "desc",
            with_counts=True,
            allowed_slugs=None,
        )
    except TypeError:
        # An older signature without the scope argument: read, then filter.
        result = list_threads_filtered(
            slugs=slugs, limit=page_size, offset=max(0, int(offset or 0)),
            order=order or "desc", with_counts=True,
        )
    except Exception as exc:
        log.warning("thread listing failed: %s", exc)
        return ThreadPage(limit=page_size, offset=max(0, int(offset or 0)))

    raw_items = result.get("items") if isinstance(result, dict) else result
    rows = list(raw_items or [])
    scoped = _scope_filter(rows, allowed_slugs)
    total = int(result.get("total") or len(scoped)) if isinstance(result, dict) else len(scoped)
    return ThreadPage(
        items=[_summary_of(row) for row in scoped],
        total=total,
        offset=max(0, int(offset or 0)),
        limit=page_size,
        truncated=bool(isinstance(result, dict) and result.get("truncated")),
    )


def get_thread(thread_id: str,
               allowed_slugs: Optional[List[str]] = None) -> ThreadSummary:
    """Return the index row of one dialog, or raise ``thread_not_found``."""
    from core.threads_devagent import load_thread_meta

    meta = {}
    try:
        meta = load_thread_meta(thread_id) or {}
    except Exception as exc:
        log.warning("thread meta read failed for %s: %s", thread_id, exc)
        meta = {}
    if not meta:
        raise errors.thread_not_found(thread_id)
    meta.setdefault("thread_id", thread_id)
    if not _scope_filter([meta], allowed_slugs):
        raise errors.thread_not_found(thread_id)
    return _summary_of(meta)


def read_messages(thread_id: str, *, offset: int = 0, limit: int = DEFAULT_LIMIT,
                  include_tool_results: bool = False,
                  allowed_slugs: Optional[List[str]] = None) -> ThreadWindow:
    """Return one window of a dialog's messages.

    A window of a thread the caller may not see raises ``thread_not_found``:
    the empty answer an unfiltered read would give is indistinguishable from
    "the thread exists but has no messages", and a caller cannot tell which
    one it received.
    """
    from core.threads_devagent import read_thread_window

    summary = get_thread(thread_id, allowed_slugs=allowed_slugs)
    page_size = _bounded_limit(limit)
    start = max(0, int(offset or 0))
    try:
        window = read_thread_window(
            thread_id, offset=start, limit=page_size,
            include_tool_results=bool(include_tool_results),
        )
    except Exception as exc:
        log.warning("thread window read failed for %s: %s", thread_id, exc)
        return ThreadWindow(thread=summary, offset=start, limit=page_size)

    window = window if isinstance(window, dict) else {}
    rows = list(window.get("messages") or [])
    messages = [_message_of(start + i, row) for i, row in enumerate(rows)]
    total = int(window.get("total") or len(messages))
    remaining = int(window.get("remaining") if window.get("remaining") is not None
                    else max(0, total - start - len(messages)))
    return ThreadWindow(thread=summary, messages=messages, total=total,
                        offset=start, limit=page_size, remaining=remaining)


def search_messages(query: str, *, slugs: Optional[List[str]] = None,
                    regex: bool = False, include_tool_results: bool = False,
                    max_results: int = 20,
                    allowed_slugs: Optional[List[str]] = None) -> SearchPage:
    """Search message bodies, restricted to the caller's scope."""
    from core.threads_devagent import search_thread_messages

    text = str(query or "").strip()
    if not text:
        raise errors.invalid_request("query is required", field="query")
    try:
        result = search_thread_messages(
            text, slugs=slugs, regex=bool(regex),
            include_tool_results=bool(include_tool_results),
            max_results=max(1, min(100, int(max_results or 20))),
            allowed_slugs=allowed_slugs,
        )
    except TypeError:
        result = search_thread_messages(
            text, slugs=slugs, regex=bool(regex),
            include_tool_results=bool(include_tool_results),
            max_results=max(1, min(100, int(max_results or 20))),
        )
    except errors.GatewayError:
        raise
    except Exception as exc:
        log.warning("message search failed: %s", exc)
        return SearchPage(query=text)
    result = result if isinstance(result, dict) else {}
    hits = list(result.get("hits") or [])
    return SearchPage(hits=hits, total=int(result.get("total") or len(hits)),
                      query=text)


def load_history(thread_id: str) -> List[Dict[str, Any]]:
    """Return the conversation of a dialog in the loop's history shape.

    This is what a continued run needs: the same role/content list the page
    keeps in its session, rebuilt from storage so a restart does not lose
    the conversation of a dialog it is asked to continue.
    """
    from core.threads_devagent import load_thread_messages

    try:
        raw = load_thread_messages(thread_id) or []
    except Exception as exc:
        log.warning("history load failed for %s: %s", thread_id, exc)
        return []
    history: List[Dict[str, Any]] = []
    for row in raw:
        row = row if isinstance(row, dict) else {}
        if str(row.get("role") or "") in ("checkpoint", ":checkpoint"):
            continue
        history.append({
            "role": str(row.get("role") or "user"),
            "content": str(row.get("content") or ""),
            "ts": str(row.get("ts") or ""),
            "file_name": str(row.get("file_name") or ""),
            "file_chars": int(row.get("file_chars") or 0),
        })
    return history


def delete_thread(thread_id: str,
                  allowed_slugs: Optional[List[str]] = None) -> bool:
    """Delete a dialog. Kept out of the HTTP surface of v1 on purpose."""
    from core.threads_devagent import delete_thread as _delete

    get_thread(thread_id, allowed_slugs=allowed_slugs)
    try:
        return bool(_delete(thread_id))
    except Exception as exc:
        log.warning("thread delete failed for %s: %s", thread_id, exc)
        return False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
