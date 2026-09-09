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
from core.files import MAX_THREAD_FILE_BYTES
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


# ─── Dialog upload files (history/<tid>/files) ────────────────────────────────
# Uploaded dialog files are stored as RAW BYTES under their original names.
# The platform never parses or extracts upload content - the orchestrator
# decides itself when and how to consume the files (read_thread_file for text,
# run_code with zipfile/PIL/... for other formats).


def _thread_files_dir(tid: str) -> str:
    """Return the files directory path of a dialog thread (no side effects)."""
    return os.path.join(get_thread_dir(tid), "files")


def _safe_thread_file_name(file_name: Any) -> str:
    """Normalize an upload name into a safe basename inside the files dir.

    Keeps the original name but strips any directory components; rejects
    invalid names so a hostile name can never escape the folder.
    """
    name = str(file_name or "").strip()
    if not name:
        raise ValueError("File name must not be empty")
    name = name.replace("\\", "/").rstrip("/")
    base = name.rsplit("/", 1)[-1].strip()
    if not base or base in (".", ".."):
        raise ValueError(f"Invalid file name: {file_name!r}")
    if "\x00" in base:
        raise ValueError("File name contains a NUL byte")
    return base


def _thread_file_path(tid: str, file_name: str) -> str:
    """Return the absolute, traversal-safe path of a thread upload by name."""
    base = _safe_thread_file_name(file_name)
    return os.path.join(_thread_files_dir(tid), base)


def save_thread_file_data(tid: str, file_name: str, data: bytes) -> str:
    """Save an uploaded dialog file as RAW BYTES into history/<tid>/files.

    The content is stored unchanged under the original file name (an existing
    file with the same name is replaced). No content extraction happens here -
    the orchestrator decides itself how to consume the file later.

    Raises ValueError when the size cap is exceeded or the name is invalid.
    Returns the absolute saved path.
    """
    if not tid or not str(tid).strip():
        raise ValueError("Thread id must not be empty")
    if not isinstance(data, (bytes, bytearray)):
        data = bytes(data or b"")
    if len(data) > MAX_THREAD_FILE_BYTES:
        raise ValueError(
            f"File exceeds the {MAX_THREAD_FILE_BYTES}-byte dialog attachment limit"
        )
    dest = _thread_file_path(tid, file_name)
    ensure_dir(os.path.dirname(dest))
    with open(dest, "wb") as fh:
        fh.write(data)
    return dest


def _looks_binary(raw: bytes, name: str = "") -> bool:
    """Classify an upload as binary for listing/reading purposes.

    A NUL byte in the head of the payload is the classic text/binary
    heuristic; well-known binary extensions cover short files whose headers
    contain no NUL (e.g. a tiny JPEG). This is presentation-only
    classification - the platform never extracts content; extraction
    decisions belong to the orchestrator.
    """
    if b"\x00" in raw[:8192]:
        return True
    lower = str(name or "").lower()
    return os.path.splitext(lower)[1] in _BINARY_EXTENSIONS


_BINARY_EXTENSIONS = {
    ".7z", ".avi", ".bin", ".bmp", ".doc", ".docx", ".dll", ".exe",
    ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".mkv", ".mov", ".mp3",
    ".mp4", ".pdf", ".png", ".ppt", ".pptx", ".pyc", ".rar", ".tar",
    ".tif", ".tiff", ".wav", ".webp", ".xls", ".xlsx", ".zip",
}


def _try_decode_text(raw: bytes, name: str = "") -> tuple:
    """Return (text, encoding) for text-looking data, else (None, None).

    The platform only classifies uploads - it never extracts content;
    extraction decisions belong to the orchestrator.
    """
    if _looks_binary(raw, name):
        return None, None
    for enc in ("utf-8", "cp1251"):
        try:
            return raw.decode(enc), enc
        except Exception:
            continue
    return None, None


def list_thread_files(tid: str) -> List[Dict[str, Any]]:
    """Return the dialog upload records of a thread (sorted by name).

    Each record: ``{name, path, bytes, is_text, probe}`` where ``probe`` is
    the first characters of the decoded text (empty for binary files). The
    platform only reports facts - it never extracts or interprets content.
    """
    files_dir = _thread_files_dir(tid)
    records = []
    try:
        names = sorted(
            n for n in os.listdir(files_dir)
            if os.path.isfile(os.path.join(files_dir, n))
        )
    except OSError:
        return records
    for name in names:
        fpath = os.path.join(files_dir, name)
        try:
            size = os.path.getsize(fpath)
            with open(fpath, "rb") as fh:
                head = fh.read(4096)
        except OSError:
            continue
        text, _enc = _try_decode_text(head, name)
        records.append({
            "name": name,
            "path": fpath,
            "bytes": size,
            "is_text": text is not None,
            "probe": (text or "")[:200],
        })
    return records


def read_thread_file(tid: str, file_name: str,
                     offset: int = 0, limit: Optional[int] = None) -> dict:
    """Read a dialog upload as TEXT with an optional line window.

    Binary files (neither utf-8 nor cp1251) are reported as ``is_text=False``
    with an empty ``content`` and a hint to process them via run_code - the
    platform never guesses extraction logic.

    Returns ``{ok, ...}``; on success includes name, path, bytes, content,
    decoded_as, offset, limit, total_lines and remaining. Errors (missing
    file, traversal attempt, oversized file) return ``{ok: False, error}``.
    """
    try:
        fpath = _thread_file_path(tid, file_name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "name": str(file_name or "")}
    if not os.path.isfile(fpath):
        return {"ok": False, "error": f"File not found in dialog files: {file_name}",
                "name": str(file_name)}
    try:
        size = os.path.getsize(fpath)
    except OSError as exc:
        return {"ok": False, "error": f"Cannot stat file: {exc}", "name": str(file_name)}
    if size > MAX_THREAD_FILE_BYTES:
        return {"ok": False, "error": f"File is too large to read as text ({size} bytes)",
                "name": str(file_name)}
    try:
        with open(fpath, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return {"ok": False, "error": f"Cannot read file: {exc}", "name": str(file_name)}
    text, enc = _try_decode_text(raw, file_name)
    if text is None:
        return {
            "ok": True,
            "name": os.path.basename(fpath),
            "path": fpath,
            "bytes": size,
            "is_text": False,
            "content": "",
            "hint": "Binary file: use run_code to process it (e.g. zipfile for archives, PIL for images).",
        }
    lines = text.split("\n")
    total_lines = len(lines)
    offset_int = max(0, int(offset or 0))
    if offset_int >= total_lines:
        return {
            "ok": True,
            "name": os.path.basename(fpath),
            "path": fpath,
            "bytes": size,
            "is_text": True,
            "content": "",
            "decoded_as": enc,
            "offset": offset_int,
            "total_lines": total_lines,
            "remaining": 0,
            "hint": "offset is beyond the end of the file",
        }
    limit_int = None if limit is None else max(1, int(limit))
    window = lines[offset_int:] if limit_int is None else lines[offset_int:offset_int + limit_int]
    remaining = total_lines - (offset_int + len(window))
    return {
        "ok": True,
        "name": os.path.basename(fpath),
        "path": fpath,
        "bytes": size,
        "is_text": True,
        "content": "\n".join(window),
        "decoded_as": enc,
        "offset": offset_int,
        "limit": limit_int,
        "total_lines": total_lines,
        "remaining": remaining,
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
