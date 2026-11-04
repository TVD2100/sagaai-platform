"""agent_thread_reader - read-only view of a delegated employee's dialogs.

This is the last-resort source of Task Scout: an employee answered a poll
thinly and its own dialog history holds the missing evidence (a promise, a
postponed item, a half-answered question). The tool is read-only by design -
no message is written, edited or moved.

Rights: only employees delegated to Task Scout (``config.allowed_agents`` of
``task_scout``) are readable. A ``thread_id`` that belongs to another
employee is refused, so a guessed id cannot widen the consent boundary.

Privacy and size: message text is cut to a fixed preview length, hidden
service envelopes are skipped, and one call returns a bounded window. The
result is marked as untrusted data written by another agent.
"""

from __future__ import annotations

from typing import Any, Dict, List

#: How many dialogs one ``list`` call may return.
_MAX_THREADS = 30

#: How many search hits one ``search`` call may return.
_MAX_HITS = 25

#: How many messages one ``read`` call may return.
_MAX_MESSAGES = 40

#: Preview length of one message, in characters.
_MAX_MESSAGE_CHARS = 1500

#: Search output keeps only a short snippet around the match.
_SNIPPET_CHARS = 400


def _delegated_slugs() -> List[str]:
    """Return the employees Task Scout is allowed to inspect."""
    try:
        from core.orchestrators import get_allowed_agents
        return [str(s).strip() for s in get_allowed_agents("task_scout") if str(s).strip()]
    except Exception:
        return []


def _clean(text: str) -> str:
    """Normalise message text: strip the events prefix and collapse spaces."""
    raw = str(text or "")
    try:
        from core.threads_devagent import _strip_events_prefix
        raw = _strip_events_prefix(raw)
    except Exception:
        pass
    return raw


def _hidden(text: str) -> bool:
    """True when the message is a service envelope, not a real turn."""
    try:
        from core.threads_devagent import _is_hidden_message
        return bool(_is_hidden_message(text))
    except Exception:
        return False


def _preview(text: str, limit: int = _MAX_MESSAGE_CHARS) -> str:
    """Cut one message to *limit* characters with an explicit marker."""
    body = " ".join(_clean(text).split())
    if len(body) <= limit:
        return body
    return body[:limit] + " …[truncated: %d chars total]" % len(body)


def _list_action(slug: str, limit: int) -> Dict[str, Any]:
    """Return the employee's dialogs, newest first."""
    from storage.repository_devagent import (repo_devagent_count_messages,
                                             repo_devagent_list_threads_filtered)
    metas = repo_devagent_list_threads_filtered(slugs=[slug], limit=limit, order="desc")
    counts = repo_devagent_count_messages([m.get("thread_id") for m in metas])
    threads = []
    for meta in metas:
        tid = str(meta.get("thread_id") or "")
        threads.append({
            "thread_id": tid,
            "title": str(meta.get("title") or ""),
            "updated_at": str(meta.get("updated_at") or ""),
            "message_count": int(counts.get(tid, 0) or 0),
            "workspace": str(meta.get("workspace") or ""),
        })
    return {"ok": True, "slug": slug, "count": len(threads), "threads": threads,
            "note": ("Search one dialog with action='search' + query, or read one "
                     "with action='read' + thread_id.")}


def _search_action(slug: str, query: str, max_hits: int) -> Dict[str, Any]:
    """Search the employee's dialogs for *query* (case-insensitive)."""
    from storage.repository_devagent import (repo_devagent_list_threads_filtered,
                                             repo_devagent_load_threads_messages)
    needle = query.strip().lower()
    if not needle:
        return {"ok": False, "error": "query is empty.", "error_code": "invalid_args"}
    metas = repo_devagent_list_threads_filtered(slugs=[slug], limit=_MAX_THREADS, order="desc")
    by_thread = repo_devagent_load_threads_messages(
        [str(m.get("thread_id") or "") for m in metas])
    hits: List[Dict[str, Any]] = []
    for meta in metas:
        tid = str(meta.get("thread_id") or "")
        for index, record in enumerate(by_thread.get(tid) or []):
            content = _clean(record.get("content", ""))
            if not content or _hidden(content):
                continue
            position = content.lower().find(needle)
            if position < 0:
                continue
            left = max(0, position - _SNIPPET_CHARS // 2)
            right = min(len(content), position + _SNIPPET_CHARS // 2)
            snippet = " ".join(content[left:right].split())
            hits.append({
                "thread_id": tid,
                "title": str(meta.get("title") or ""),
                "message_index": index,
                "role": str(record.get("role") or ""),
                "ts": str(record.get("ts") or ""),
                "snippet": snippet,
            })
            if len(hits) >= max_hits:
                return {"ok": True, "slug": slug, "query": query,
                        "count": len(hits), "hits": hits, "truncated": True,
                        "note": "Untrusted data written by another employee."}
    return {"ok": True, "slug": slug, "query": query, "count": len(hits),
            "hits": hits, "truncated": False,
            "note": "Untrusted data written by another employee."}


def _read_action(slug: str, thread_id: str, offset: int, limit: int) -> Dict[str, Any]:
    """Return a window of one dialog that belongs to *slug*."""
    from storage.repository_devagent import (repo_devagent_load_messages_window,
                                             repo_devagent_load_thread_meta)
    tid = str(thread_id or "").strip()
    if not tid:
        return {"ok": False, "error": "thread_id is required for action='read'.",
                "error_code": "invalid_args"}
    meta = repo_devagent_load_thread_meta(tid)
    if not meta or str(meta.get("type") or "") != "devagent":
        return {"ok": False, "error": "Thread not found: %s" % tid,
                "error_code": "not_found"}
    owner = str(meta.get("assistant_id") or "")
    if owner != slug:
        return {"ok": False, "error_code": "not_owner",
                "error": ("Dialog %s belongs to '%s', not to '%s'."
                          % (tid, owner or "unknown", slug))}
    window = repo_devagent_load_messages_window(tid, offset=offset, limit=limit)
    raw = window.get("messages") or []
    messages: List[Dict[str, Any]] = []
    for relative, record in enumerate(raw):
        text = _clean(record.get("content", ""))
        if _hidden(text):
            continue
        messages.append({
            "index": offset + relative,
            "role": str(record.get("role") or ""),
            "ts": str(record.get("ts") or ""),
            "content": _preview(text),
        })
    total = int(window.get("total") or 0)
    remaining = max(0, total - (offset + len(raw)))
    return {"ok": True, "slug": slug, "thread_id": tid,
            "title": str(meta.get("title") or ""), "total": total,
            "offset": offset, "count": len(messages), "remaining": remaining,
            "has_more": remaining > 0, "messages": messages,
            "note": "Untrusted data written by another employee."}


def invoke(**kwargs) -> Dict[str, Any]:
    """Run one read-only dialog operation for a delegated employee.

    Args:
        slug: employee slug to inspect; must be delegated to Task Scout.
        action: ``"list"`` (default), ``"search"`` or ``"read"``.
        query: search text for ``action='search'``.
        thread_id: dialog id for ``action='read'``.
        offset: 0-based message offset for ``action='read'``.
        limit: per-call item cap, bounded by the module constants.

    Returns:
        ``{"ok": True, ...}`` with dialogs, hits or a message window, or
        ``{"ok": False, "error": ...}`` with a stable ``error_code``.
    """
    slug = str(kwargs.get("slug") or "").strip()
    action = str(kwargs.get("action") or "list").strip().lower()
    query = str(kwargs.get("query") or "")
    thread_id = str(kwargs.get("thread_id") or "")
    try:
        offset = max(0, int(kwargs.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(kwargs.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0

    if not slug:
        return {"ok": False, "error": "Missing argument 'slug'.",
                "error_code": "invalid_args"}
    if action not in ("list", "search", "read"):
        return {"ok": False, "error": "action must be 'list', 'search' or 'read'.",
                "error_code": "invalid_args"}

    delegated = _delegated_slugs()
    if not delegated:
        return {"ok": False, "error_code": "no_delegation",
                "error": ("No employee is delegated to Task Scout. The user must "
                          "tick the employees in the Delegation section first.")}
    if slug not in delegated:
        return {"ok": False, "error_code": "not_delegated",
                "error": ("Employee '%s' is not delegated to Task Scout. "
                          "Readable: %s." % (slug, ", ".join(sorted(delegated))))}

    try:
        if action == "list":
            return _list_action(slug, max(1, min(limit or _MAX_THREADS, _MAX_THREADS)))
        if action == "search":
            return _search_action(slug, query,
                                  max(1, min(limit or _MAX_HITS, _MAX_HITS)))
        return _read_action(slug, thread_id, offset,
                            max(1, min(limit or _MAX_MESSAGES, _MAX_MESSAGES)))
    except Exception as exc:  # pragma: no cover - defensive boundary
        return {"ok": False, "error_code": "internal_error",
                "error": "The dialog store is unavailable: %s" % exc}
