# -*- coding: utf-8 -*-
"""
core.threads_devagent - DevAgent thread persistence (separate DB).

Uses the isolated ``devagent.db`` database via ``storage.repository_devagent``.
Follows the same simple pattern as ``core.threads.py`` for chat threads:
  - DB is the single source of truth.
  - Messages are stored with embedded ``_events`` / ``_event_start`` / ``_event_end``
    / ``_tokens`` keys using the same JSON-prefix encoding as ``core.threads.py``.
  - ``append_thread_message`` adds one message at a time (no full-history rewrite).

Each thread is associated with an orchestrator via its slug/name (stored in
assistant_id / assistant_name columns).

Since the workspace-restoration feature, each thread also persists the LAST
active workspace (``workspace`` / ``target_file``) so reopening a saved dialog
switches DevAgent back to the correct project folder.
"""
import os, json, re, uuid, shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from storage.repository_devagent import (
    repo_devagent_create_thread,
    repo_devagent_save_thread_meta,
    repo_devagent_load_thread_meta,
    repo_devagent_save_thread_messages,
    repo_devagent_load_thread_messages,
    repo_devagent_delete_thread,
    repo_devagent_list_threads,
    repo_devagent_append_message,
    repo_devagent_delete_all_threads,
)
from core.fs import ensure_dir
from core.paths import get_thread_dir, get_thread_file_path

_PREFIX_MARKER = "__DEVAGENT_EVENTS__"
_PREFIX_PATTERN = re.compile(
    r"^" + re.escape(_PREFIX_MARKER) + r"(\{.*?\})\n", re.DOTALL
)


def _sanitize_title(title: Any) -> str:
    """Convert any title value to a safe string."""
    if title is None:
        return ""
    if hasattr(title, 'strip'):
        try:
            result = title.strip()
            if isinstance(result, str):
                return result
        except Exception:
            pass
    return str(title) if title else ""


def create_devagent_thread(title: str = "",
                           orchestrator_slug: str = None,
                           orchestrator_name: str = "DevAgent",
                           workspace: str = None,
                           target_file: str = None) -> str:
    """
    Create a new DevAgent thread associated with an orchestrator.

    Args:
        title: First user message (used as thread title).
        orchestrator_slug: The slug of the orchestrator (stored in assistant_id).
        orchestrator_name: Display name of the orchestrator (stored in assistant_name).
        workspace: Active project folder at thread creation (stored as-is).
        target_file: Optional single-file target (stored as-is).

    Returns the new thread_id.
    """
    safe_title = _sanitize_title(title)
    thread_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + str(uuid.uuid4())[:6]
    ensure_dir(os.path.join(get_thread_dir(thread_id), "files"))
    repo_devagent_create_thread(
        thread_id,
        title=safe_title[:60] if safe_title else "",
        orchestrator_slug=orchestrator_slug or None,
        orchestrator_name=orchestrator_name or "DevAgent",
        workspace=workspace or None,
        target_file=target_file or None,
    )
    return thread_id


def save_thread_workspace(tid: str, workspace: str, target_file: str = None) -> bool:
    """Persist the LAST active workspace / target_file for an existing thread.

    This is called after each agent step (or when the agent explicitly switches
    projects) so the most recent workspace is restored when the dialog is
    reopened from history.

    Returns True on success, False if the thread does not exist or on DB error.
    """
    if not tid:
        return False
    meta: Dict[str, Any] = {}
    if workspace is not None:
        meta["workspace"] = workspace or None
    if target_file is not None:
        meta["target_file"] = target_file or None
    if not meta:
        return False
    return repo_devagent_save_thread_meta(tid, meta)


def load_thread_messages(tid: str) -> List[Dict[str, Any]]:
    """Load messages from DB and restore embedded events."""
    raw = repo_devagent_load_thread_messages(tid)
    return _restore_events(raw)


def _restore_events(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip event prefix from content and restore _events / _event_* / _tokens keys."""
    out: List[Dict[str, Any]] = []
    for m in messages:
        content = m.get("content", "")
        m = dict(m)
        m.pop("_events", None)
        m.pop("_event_start", None)
        m.pop("_event_end", None)
        m.pop("_tokens", None)
        if content and content.startswith(_PREFIX_MARKER):
            mch = _PREFIX_PATTERN.match(content)
            if mch:
                try:
                    event_data = json.loads(mch.group(1))
                except Exception:
                    event_data = {}
                content = content[mch.end():]
                if "_events" in event_data:
                    m["_events"] = event_data["_events"]
                if "_event_start" in event_data:
                    m["_event_start"] = event_data["_event_start"]
                if "_event_end" in event_data:
                    m["_event_end"] = event_data["_event_end"]
                if "tokens" in event_data:
                    m["_tokens"] = event_data["tokens"]
                if "_tokens" in event_data:
                    m["_tokens"] = event_data["_tokens"]
        m["content"] = content
        out.append(m)
    return out


def save_thread_messages(tid: str, messages: List[Dict[str, Any]]) -> None:
    """
    Persist the full list of messages for a thread.

    Transient keys (_events, _event_start, _event_end, _tokens) are embedded as a
    JSON prefix in the content field so they survive the DB round-trip.
    """
    allowed_keys = {"role", "content", "ts", "file_name", "file_chars"}
    clean = []
    for m in messages:
        clean_msg = {k: v for k, v in m.items() if k in allowed_keys}
        events = m.get("_events")
        event_start = m.get("_event_start")
        event_end = m.get("_event_end")
        tokens = m.get("_tokens")
        if events or event_start is not None or event_end is not None or tokens:
            event_data: Dict[str, Any] = {}
            if events:
                event_data["_events"] = events
            if event_start is not None:
                event_data["_event_start"] = event_start
            if event_end is not None:
                event_data["_event_end"] = event_end
            if tokens:
                event_data["tokens"] = tokens
            prefix = _PREFIX_MARKER + json.dumps(event_data, ensure_ascii=False) + "\n"
            clean_msg["content"] = prefix + clean_msg.get("content", "")
        clean.append(clean_msg)
    repo_devagent_save_thread_messages(tid, clean)


def append_thread_message(tid: str, role: str, content: str,
                          file_name: str = "", file_chars: int = 0,
                          events: Optional[List[Dict[str, Any]]] = None,
                          tokens: Optional[Dict[str, int]] = None) -> None:
    """Append a single message to a DevAgent thread.

    If ``events`` is provided, they are embedded as a JSON prefix in the
    content field (same encoding as ``save_thread_messages``).
    If ``tokens`` is provided (dict with 'in'/'out'/'cache' keys), it is embedded too.
    """
    final_content = content
    event_data: Dict[str, Any] = {}
    if events:
        event_data["_events"] = events
    if tokens:
        event_data["tokens"] = tokens
    if event_data:
        prefix = _PREFIX_MARKER + json.dumps(event_data, ensure_ascii=False) + "\n"
        final_content = prefix + final_content
    repo_devagent_append_message(tid, role, final_content,
                                 file_name=file_name, file_chars=file_chars)


def sum_thread_tokens(messages: List[Dict[str, Any]]) -> tuple:
    """Return (sum_in_tokens, sum_out_tokens, sum_cache_tokens) for messages.

    ``sum_cache_tokens`` is the cumulative count of cached input tokens
    reported by the provider (0 when the provider doesn't report cache).
    """
    total_in = 0
    total_out = 0
    total_cache = 0
    for m in messages:
        tokens = m.get("_tokens") or {}
        if isinstance(tokens, dict):
            total_in += int(tokens.get("in", 0) or 0)
            total_out += int(tokens.get("out", 0) or 0)
            total_cache += int(tokens.get("cache", 0) or 0)
    return total_in, total_out, total_cache


def load_thread_meta(tid: str) -> dict:
    return repo_devagent_load_thread_meta(tid) or {}


def delete_thread(tid: str) -> None:
    """Delete a DevAgent thread and all its files from disk."""
    repo_devagent_delete_thread(tid)
    tdir = get_thread_dir(tid)
    if os.path.isdir(tdir):
        shutil.rmtree(tdir)


def list_devagent_threads(slug: str = None) -> List[Dict[str, Any]]:
    """Return list of DevAgent thread metadata.

    If ``slug`` is None, returns ALL threads. Otherwise only threads for
    the given orchestrator slug.
    """
    return repo_devagent_list_threads(slug)


def list_orchestrator_threads(slug: str) -> List[Dict[str, Any]]:
    """Return list of threads for a specific orchestrator slug."""
    return repo_devagent_list_threads(slug)


def delete_all_devagent_threads(slug: str = None) -> None:
    """Delete DevAgent threads from DB. If slug given, only that orchestrator's."""
    repo_devagent_delete_all_threads(slug)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
