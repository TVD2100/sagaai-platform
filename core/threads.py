# -*- coding: utf-8 -*-
"""
core.threads - thread (chat session) persistence.

Each thread has an assistant_id + assistant_name and stores messages as JSON.
DevAgent threads use thread_type="devagent".

DevAgent event persistence:
  - _events, _event_start, _event_end, _tokens are embedded in the content
    field as a JSON prefix (``__DEVAGENT_EVENTS__{...}\n``) because the Message
    model has no dedicated columns for them.
  - ``load_thread_messages`` strips this prefix and restores the keys.
  - ``save_thread_messages`` injects the prefix for any message that has them.
"""
import os, json, re, uuid, shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from storage.repository import (
    repo_create_thread, repo_save_thread_meta, repo_load_thread_meta,
    repo_save_thread_messages, repo_load_thread_messages,
    repo_delete_thread, repo_list_threads_by_type, repo_list_chat_threads,
    repo_append_message, repo_list_all_threads,
)
from core.fs import ensure_dir
from core.paths import get_thread_dir, get_thread_file_path

_PREFIX_MARKER = "__DEVAGENT_EVENTS__"
_PREFIX_PATTERN = re.compile(
    r"^" + re.escape(_PREFIX_MARKER) + r"(\{.*?\})\n", re.DOTALL
)


def get_thread_messages(tid: str) -> List[Dict[str, Any]]:
    raw = repo_load_thread_messages(tid)
    return _restore_events(raw)


def _sanitize_title(title: Any) -> str:
    """Convert any title value to a safe string.

    Handles mock objects and other non-string values gracefully.
    """
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


def create_devagent_thread(title: str = "") -> str:
    """
    Create a new DevAgent thread with a given title (first user message).
    No assistant association - DevAgent uses its own model config.
    """
    safe_title = _sanitize_title(title)
    thread_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + str(uuid.uuid4())[:6]
    ensure_dir(os.path.join(get_thread_dir(thread_id), "files"))
    repo_create_thread(thread_id, "", "DevAgent", thread_type="devagent")
    if safe_title:
        repo_save_thread_meta(thread_id, {"title": safe_title[:60]})
    return thread_id


def create_thread(assistant_id: str, assistant_name: str, title: str = "", **kwargs) -> str:
    """Create a regular chat thread. Returns the thread_id."""
    # Backward compatibility: accept the legacy 'skill_id' / 'skill_name'
    # keyword names and ignore any extra keywords.
    if 'skill_id' in kwargs and assistant_id is None:
        assistant_id = kwargs.pop('skill_id')
    if 'skill_name' in kwargs and (not assistant_name or assistant_name == ''):
        assistant_name = kwargs.pop('skill_name')
    kwargs.pop('skill_id', None)
    kwargs.pop('skill_name', None)
    safe_title = _sanitize_title(title) if title else ""
    thread_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + str(uuid.uuid4())[:6]
    ensure_dir(os.path.join(get_thread_dir(thread_id), "files"))
    repo_create_thread(thread_id, assistant_id, assistant_name)
    if safe_title:
        repo_save_thread_meta(thread_id, {"title": safe_title[:60]})
    return thread_id


def load_thread_messages(tid: str) -> List[Dict[str, Any]]:
    return get_thread_messages(tid)


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
    repo_save_thread_messages(tid, clean)


def append_thread_message(tid: str, role: str, content: str,
                          file_name: str = "", file_chars: int = 0,
                          tokens: Optional[Dict[str, int]] = None) -> None:
    """Append a single message to a thread, auto-setting title from first user message.

    If ``tokens`` is provided (dict with 'in'/'out' keys), it is embedded as a
    JSON prefix in the content field (same encoding as ``save_thread_messages``).
    """
    final_content = content
    if tokens:
        prefix = _PREFIX_MARKER + json.dumps({"tokens": tokens}, ensure_ascii=False) + "\n"
        final_content = prefix + final_content
    repo_append_message(tid, role, final_content, file_name=file_name, file_chars=file_chars)


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


def save_thread_file(tid: str, file_name: str, content: str) -> str:
    """Persist uploaded file content. Returns the saved path."""
    fdir  = os.path.join(get_thread_dir(tid), "files")
    ensure_dir(fdir)
    fpath = os.path.join(fdir, f"{file_name}.txt")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(content)
    return fpath


def load_thread_file(tid: str, file_name: str) -> Optional[str]:
    """Load previously saved thread file. Returns None if not found."""
    fpath = get_thread_file_path(tid, file_name)
    if not os.path.exists(fpath):
        return None
    with open(fpath, "r", encoding="utf-8") as f:
        return f.read()


def load_thread_meta(tid: str) -> dict:
    return repo_load_thread_meta(tid) or {}


def delete_thread(tid: str) -> None:
    """Delete a thread and all its files."""
    repo_delete_thread(tid)
    tdir = get_thread_dir(tid)
    if os.path.isdir(tdir):
        shutil.rmtree(tdir)


def list_devagent_threads() -> List[Dict[str, Any]]:
    """Return list of DevAgent thread metadata (thread_id, title, updated_at, …)."""
    return repo_list_threads_by_type("devagent")


def list_chat_threads() -> List[Dict[str, Any]]:
    """Return list of regular chat thread metadata."""
    return repo_list_chat_threads()


def list_all_threads() -> List[Dict[str, Any]]:
    """Return all chat threads."""
    return repo_list_all_threads()


def messages_to_api_history(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Convert internal messages (with file attachments) to the API history format."""
    result = []
    for m in messages:
        role = m.get("role") or ""
        content = m.get("content") or ""
        fname = m.get("file_name", "")
        if fname and role == "user":
            suffix = f"\n\n[Attached file: {fname}]"
            result.append({"role": role, "content": content + suffix})
        else:
            result.append({"role": role, "content": content})
    return result
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
