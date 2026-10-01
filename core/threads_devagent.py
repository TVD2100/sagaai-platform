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

Since the thread-search feature, this module also hosts the read side of the
dialog tools: ``search_thread_messages`` (content search across dialogs),
``list_threads_filtered`` (dialog listing with per-dialog message counters)
and ``read_thread_window`` (windowed view of one dialog). Access control
(which orchestrator may see which dialog) is enforced one layer above, in the
tool wrappers of ``dev_agent.universal_agent``.
"""
import os, json, re, uuid, shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from storage.repository_devagent import (
    repo_devagent_create_thread,
    repo_devagent_save_thread_meta,
    repo_devagent_clear_thread_workspace,
    repo_devagent_load_thread_meta,
    repo_devagent_save_thread_messages,
    repo_devagent_load_thread_messages,
    repo_devagent_delete_thread,
    repo_devagent_list_threads,
    repo_devagent_append_message,
    repo_devagent_delete_all_threads,
    repo_devagent_list_threads_filtered,
    repo_devagent_count_messages,
    repo_devagent_load_threads_messages,
    repo_devagent_load_messages_window,
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


# Orchestrator slug of the built-in developer: the only employee allowed to
# keep platform folders (the SagaAI install root) in its dialogs' meta.
_DEVAGENT_SLUG = "dev_agent"


def _is_platform_path(raw, install_root: str) -> bool:
    """True when *raw* points at the platform root or a folder inside it."""
    text = str(raw or "").strip()
    if not text:
        return False
    try:
        resolved = os.path.realpath(os.path.expanduser(text))
    except Exception:
        return False
    return resolved == install_root or resolved.startswith(install_root + os.sep)


def migrate_platform_workspace_meta() -> int:
    """One-time cleanup of leaked platform folders in foreign thread meta.

    Workspace isolation v2: dialogs of employees other than the built-in
    DevAgent must never keep the SagaAI install root (or a path inside it)
    as their saved workspace - reopening such a dialog would silently
    switch the agent back to the platform folder (the historical leak).
    Matching threads lose ``workspace`` / ``target_file`` and reopen in
    the empty state, where the user explicitly picks a folder.

    dev_agent threads are skipped (DevAgent legitimately develops the
    platform itself), as are threads without an orchestrator slug
    (pre-orchestrator legacy rows - most likely old DevAgent dialogs).
    ``updated_at`` is kept intact, so a maintenance pass never reorders
    the dialog list.

    Idempotent and best effort: returns the number of cleaned threads, 0
    when the database is clean or unavailable; never raises.
    """
    try:
        from dev_agent import config as dagent_config
        install_root = os.path.realpath(str(dagent_config.INSTALL_ROOT))
    except Exception:
        return 0
    try:
        metas = repo_devagent_list_threads(None)
    except Exception:
        return 0
    cleaned = 0
    for meta in metas:
        slug = str(meta.get("assistant_id") or "").strip()
        if slug in ("", _DEVAGENT_SLUG):
            continue  # dev_agent or pre-orchestrator legacy dialog
        leaked = any(
            _is_platform_path(meta.get(field), install_root)
            for field in ("workspace", "target_file")
        )
        if leaked and repo_devagent_clear_thread_workspace(meta.get("thread_id", "")):
            cleaned += 1
    return cleaned


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
        # M2: compact hidden tool_results before persisting (context-overflow
        # protection): a giant tool_result payload must never be stored raw.
        if clean_msg.get("role") == "user":
            try:
                from dev_agent.agent_loop import summarize_tool_result_for_storage
                clean_msg["content"] = summarize_tool_result_for_storage(
                    clean_msg.get("content", "") or ""
                )
            except Exception:
                pass
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
    # M2: compact hidden tool_results before persisting (context-overflow
    # protection): a giant tool_result payload must never be stored raw.
    if role == "user":
        try:
            from dev_agent.agent_loop import summarize_tool_result_for_storage
            final_content = summarize_tool_result_for_storage(final_content)
        except Exception:
            pass
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
    # Forget the in-memory workspace binding as well: a deleted dialog must
    # never keep handing its folder to other or future dialogs.
    try:
        from dev_agent.workspace_binding import clear_thread
        clear_thread(tid)
    except Exception:
        pass


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
# ─── Thread-search service layer ─────────────────────────────────────────────
# Read side of the dialog tools (search_in_threads / list_threads /
# read_thread exposed by dev_agent.universal_agent). Hidden service messages
# (tool-result envelopes, AUTO_CONTINUE prompts) are identified by their
# content prefixes, duplicated here to keep this module free of the heavy
# agent-loop import. Access control lives one layer above, in the wrappers.

_HIDDEN_PREFIXES = ('{"tool_result"', "AUTO_CONTINUE:")

MAX_SEARCH_THREADS = 200     # cap on dialogs scanned per search call
MAX_SEARCH_MESSAGES = 5000   # cap on stored messages scanned per search call
MAX_SEARCH_HITS = 100        # cap on returned hits
MAX_THREADS_PER_LIST = 100   # cap on list_threads page size
MAX_READ_LIMIT = 200         # cap on read_thread page size
MAX_SNIPPET_CHARS = 300      # default snippet width in search hits
MAX_MESSAGE_CHARS = 8000     # per-message content cap in read_thread


def _normalize_date_bound(value, end_of_day: bool = False):
    """Normalize a ``YYYY-MM-DD`` or ISO-8601 bound to a comparable ISO string.

    A plain date becomes 00:00:00 (start) or 23:59:59.999999 (end of day) so
    an inclusive lexicographic comparison against the stored ISO timestamps
    works. Returns None for empty or unparsable input (treated as unbounded).
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None
        return raw + ("T23:59:59.999999" if end_of_day else "T00:00:00")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.isoformat()


def _strip_events_prefix(content: str) -> str:
    """Drop the embedded JSON events prefix from a stored message content."""
    text = content or ""
    if text.startswith(_PREFIX_MARKER):
        mch = _PREFIX_PATTERN.match(text)
        if mch:
            return text[mch.end():]
    return text


def _is_hidden_message(content: str) -> bool:
    """True for service messages users never typed (tool results, autoprompts)."""
    text = _strip_events_prefix(content or "").lstrip()
    return text.startswith(_HIDDEN_PREFIXES)


def _locate_match(content: str, query: str, pattern=None):
    """Return (start, end, match_count) of the first match, or None."""
    if pattern is not None:
        start = None
        end = None
        count = 0
        for m in pattern.finditer(content):
            if start is None:
                start, end = m.start(), m.end()
            count += 1
        if start is None:
            return None
        return start, end, count
    low = content.lower()
    q = (query or "").lower()
    if not q:
        return None
    start = low.find(q)
    if start < 0:
        return None
    return start, start + len(q), low.count(q)


def _make_snippet(content: str, start: int, end: int,
                  width: int = MAX_SNIPPET_CHARS) -> str:
    """Return a whitespace-collapsed fragment around the match position."""
    width = max(40, min(int(width or MAX_SNIPPET_CHARS), 600))
    half = max(0, (width - (end - start)) // 2)
    left = max(0, start - half)
    right = min(len(content), end + half)
    text = " ".join(content[left:right].strip().split())
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(content) else ""
    return prefix + text + suffix


def _message_preview(record: Dict[str, Any], index: int,
                     include_tool_results: bool):
    """Build one output message dict (or None when the message is hidden)."""
    content = _strip_events_prefix(record.get("content", "") or "")
    if not include_tool_results and _is_hidden_message(content):
        return None
    total_chars = len(content)
    if total_chars > MAX_MESSAGE_CHARS:
        content = content[:MAX_MESSAGE_CHARS] + (
            f"\n…[truncated: {total_chars} chars total]"
        )
    return {
        "index": index,
        "role": record.get("role", ""),
        "ts": record.get("ts", ""),
        "content": content,
        "file_name": record.get("file_name", "") or "",
        "file_chars": int(record.get("file_chars", 0) or 0),
    }
def search_thread_messages(query: str,
                           thread_ids: List[str] = None,
                           slugs: List[str] = None,
                           date_from=None,
                           date_to=None,
                           date_field: str = "updated",
                           role: str = None,
                           regex: bool = False,
                           include_tool_results: bool = False,
                           max_results: int = 50,
                           snippet_chars: int = MAX_SNIPPET_CHARS,
                           allowed_slugs: List[str] = None) -> Dict[str, Any]:
    """Search the stored messages of dialog threads.

    Targeting (first match wins):
      * ``thread_ids`` - explicit dialogs (e.g. the current thread id);
      * ``slugs`` + optional date range - every dialog of those orchestrators;
      * nothing - every dialog (the tool wrapper narrows this for
        non-DevAgent orchestrators via ``allowed_slugs``).

    Hidden service messages (tool-result envelopes, AUTO_CONTINUE prompts)
    are skipped unless ``include_tool_results`` is true. Matching is
    case-insensitive; ``regex=True`` interprets the query as a regular
    expression (compiled with re.IGNORECASE). Each hit carries thread_id,
    title, orchestrator, message_index (absolute position inside the dialog),
    role, ts, a snippet around the first match and match_count.

    ``allowed_slugs`` enforces access control: None = no restriction
    (DevAgent); a list = only dialogs of those orchestrators. Explicit
    thread_ids outside the list are reported in ``denied_threads``; when
    nothing remains searchable the call fails with an access-denied error.
    """
    q = str(query or "").strip()
    if not q:
        return {"ok": False, "error": "Missing required argument 'query'."}
    if date_field not in ("updated", "created"):
        return {"ok": False,
                "error": "date_field must be 'updated' or 'created'."}
    bound_from = _normalize_date_bound(date_from)
    bound_to = _normalize_date_bound(date_to, end_of_day=True)
    role_filter = str(role or "").strip().lower() or None
    if role_filter and role_filter not in ("user", "assistant", "system"):
        return {"ok": False,
                "error": "role must be one of: user, assistant, system."}
    pattern = None
    if regex:
        try:
            pattern = re.compile(q, re.IGNORECASE)
        except re.error as exc:
            return {"ok": False, "error": f"Invalid regular expression: {exc}"}
    try:
        max_results = max(1, min(int(max_results or 50), MAX_SEARCH_HITS))
    except (TypeError, ValueError):
        max_results = 50

    explicit_ids: List[str] = []
    if thread_ids:
        raw_ids = thread_ids if isinstance(thread_ids, (list, tuple)) else [thread_ids]
        for item in raw_ids:
            tid = str(item or "").strip()
            if tid and tid not in explicit_ids:
                explicit_ids.append(tid)
        if len(explicit_ids) > 20:
            return {"ok": False,
                    "error": "Too many thread_ids (max 20 per call)."}

    denied_threads: List[str] = []
    missing_threads: List[str] = []
    metas: List[Dict[str, Any]] = []
    allowed_set = set(allowed_slugs) if allowed_slugs else None

    if explicit_ids:
        for tid in explicit_ids:
            meta = repo_devagent_load_thread_meta(tid)
            if not meta or meta.get("type") != "devagent":
                missing_threads.append(tid)
                continue
            if allowed_set is not None and (meta.get("assistant_id") or "") not in allowed_set:
                denied_threads.append(tid)
                continue
            metas.append(meta)
        if not metas:
            if denied_threads:
                return {"ok": False,
                        "error": ("Access denied: the requested dialog(s) belong "
                                  "to another orchestrator."),
                        "denied_threads": denied_threads,
                        "missing_threads": missing_threads}
            return {"ok": False,
                    "error": "Thread not found: " + ", ".join(missing_threads),
                    "missing_threads": missing_threads}
        scope_label = "thread" if len(metas) == 1 else f"threads:{len(metas)}"
    else:
        effective_slugs = slugs or None
        access_limited = False
        if allowed_set is not None:
            if effective_slugs:
                filtered = [s for s in effective_slugs if s in allowed_set]
                if not filtered:
                    return {"ok": False,
                            "error": ("Access denied: the requested "
                                      "orchestrator(s) are not accessible.")}
                effective_slugs = filtered
            else:
                effective_slugs = sorted(allowed_set)
            access_limited = True
        metas = repo_devagent_list_threads_filtered(
            slugs=effective_slugs,
            date_from=bound_from,
            date_to=bound_to,
            date_field=date_field,
            limit=MAX_SEARCH_THREADS,
            order="desc",
        )
        if effective_slugs:
            scope_label = ("orchestrator:" + effective_slugs[0]
                           if len(effective_slugs) == 1
                           else "orchestrators:" + ",".join(effective_slugs))
        else:
            scope_label = "all"
        if access_limited:
            scope_label += " (access-limited)"

    by_thread = repo_devagent_load_threads_messages(
        [m.get("thread_id") for m in metas])
    hits: List[Dict[str, Any]] = []
    messages_scanned = 0
    truncated = False
    for meta in metas:
        tid = meta.get("thread_id", "")
        records = by_thread.get(tid) or []
        for idx, rec in enumerate(records):
            if messages_scanned >= MAX_SEARCH_MESSAGES or len(hits) >= max_results:
                truncated = True
                break
            content = _strip_events_prefix(rec.get("content", "") or "")
            if not include_tool_results and _is_hidden_message(content):
                continue
            if role_filter and (rec.get("role") or "") != role_filter:
                continue
            messages_scanned += 1
            located = _locate_match(content, q, pattern)
            if not located:
                continue
            start, end, match_count = located
            hits.append({
                "thread_id": tid,
                "title": meta.get("title", "") or "",
                "orchestrator": meta.get("assistant_name", "") or "",
                "orchestrator_slug": meta.get("assistant_id", "") or "",
                "thread_updated_at": meta.get("updated_at", ""),
                "message_index": idx,
                "role": rec.get("role", ""),
                "ts": rec.get("ts", ""),
                "snippet": _make_snippet(content, start, end, snippet_chars),
                "match_count": match_count,
            })
        if truncated:
            break
    return {
        "ok": True,
        "query": q,
        "scope": scope_label,
        "threads_scanned": len(metas),
        "messages_scanned": messages_scanned,
        "count": len(hits),
        "hits": hits,
        "missing_threads": missing_threads,
        "denied_threads": denied_threads,
        "truncated": truncated,
    }
def list_threads_filtered(slugs: List[str] = None,
                          date_from=None,
                          date_to=None,
                          date_field: str = "updated",
                          limit: int = 50,
                          offset: int = 0,
                          order: str = "desc",
                          with_counts: bool = True,
                          allowed_slugs: List[str] = None) -> Dict[str, Any]:
    """List dialog threads with metadata and (optionally) message counts.

    Filters mirror ``search_thread_messages``: optional ``slugs`` (orchestrator
    slugs), an inclusive date range over ``date_field``, pagination via
    ``offset``/``limit`` (capped at MAX_THREADS_PER_LIST) and sort order.

    ``allowed_slugs`` enforces access control the same way (None = no
    restriction for DevAgent; a list = only those orchestrators' dialogs).
    ``truncated`` is a precise page-full indicator: it is computed by
    fetching one row beyond the page. Each thread item carries created_at /
    updated_at / workspace and, when ``with_counts``, message_count.
    """
    if date_field not in ("updated", "created"):
        return {"ok": False,
                "error": "date_field must be 'updated' or 'created'."}
    if order not in ("asc", "desc"):
        return {"ok": False, "error": "order must be 'asc' or 'desc'."}
    try:
        limit = max(1, min(int(limit or 50), MAX_THREADS_PER_LIST))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    bound_from = _normalize_date_bound(date_from)
    bound_to = _normalize_date_bound(date_to, end_of_day=True)

    effective_slugs = [str(s) for s in slugs] if slugs else None
    access_limited = False
    allowed_set = set(allowed_slugs) if allowed_slugs else None
    if allowed_set is not None:
        if effective_slugs:
            filtered = [s for s in effective_slugs if s in allowed_set]
            if not filtered:
                return {"ok": False,
                        "error": ("Access denied: the requested "
                                  "orchestrator(s) are not accessible.")}
            effective_slugs = filtered
        else:
            effective_slugs = sorted(allowed_set)
        access_limited = True

    metas = repo_devagent_list_threads_filtered(
        slugs=effective_slugs,
        date_from=bound_from,
        date_to=bound_to,
        date_field=date_field,
        limit=limit + 1,          # one extra row -> precise page-full flag
        offset=offset,
        order=order,
    )
    truncated = len(metas) > limit
    metas = metas[:limit]
    counts = (repo_devagent_count_messages([m.get("thread_id") for m in metas])
              if with_counts else {})
    items = []
    for meta in metas:
        item = {
            "thread_id": meta.get("thread_id", ""),
            "title": meta.get("title", "") or "",
            "orchestrator": meta.get("assistant_name", "") or "",
            "orchestrator_slug": meta.get("assistant_id", "") or "",
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
            "workspace": meta.get("workspace", "") or "",
        }
        if with_counts:
            item["message_count"] = counts.get(item["thread_id"], 0)
        items.append(item)

    scope_label = "all"
    if effective_slugs:
        scope_label = ("orchestrator:" + effective_slugs[0]
                       if len(effective_slugs) == 1
                       else "orchestrators:" + ",".join(effective_slugs))
    if access_limited:
        scope_label += " (access-limited)"
    return {
        "ok": True,
        "scope": scope_label,
        "offset": offset,
        "limit": limit,
        "count": len(items),
        "threads": items,
        "truncated": truncated,
    }


def read_thread_window(thread_id: str,
                       offset: int = 0,
                       limit: int = 50,
                       include_tool_results: bool = False,
                       allowed_slugs: List[str] = None) -> Dict[str, Any]:
    """Return a window of one dialog's messages (for viewing a found thread).

    Messages are previews built by ``_message_preview``: hidden service
    messages are skipped unless ``include_tool_results`` is true, content is
    capped, and each message keeps its ABSOLUTE index inside the dialog
    (stable across filtering, works as a navigation anchor).

    ``allowed_slugs`` enforces access control: None = any dialog (DevAgent);
    a list = only dialogs of those orchestrators. Unknown thread ids and
    dialogs of other orchestrators both produce a clean error.
    """
    tid = str(thread_id or "").strip()
    if not tid:
        return {"ok": False, "error": "Missing required argument 'thread_id'."}
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = max(1, min(int(limit or 50), MAX_READ_LIMIT))
    except (TypeError, ValueError):
        limit = 50
    meta = repo_devagent_load_thread_meta(tid)
    if not meta or meta.get("type") != "devagent":
        return {"ok": False, "error": f"Thread not found: {tid}"}
    if allowed_slugs is not None and (meta.get("assistant_id") or "") not in set(allowed_slugs):
        return {"ok": False,
                "error": "Access denied: this dialog belongs to another "
                         "orchestrator."}
    res = repo_devagent_load_messages_window(tid, offset=offset, limit=limit)
    raw = res.get("messages") or []
    total = int(res.get("total") or 0)
    messages = []
    for rel_i, rec in enumerate(raw):
        preview = _message_preview(rec, offset + rel_i, include_tool_results)
        if preview is not None:
            messages.append(preview)
    remaining = max(0, total - (offset + len(raw)))
    return {
        "ok": True,
        "thread_id": tid,
        "title": meta.get("title", "") or "",
        "orchestrator": meta.get("assistant_name", "") or "",
        "orchestrator_slug": meta.get("assistant_id", "") or "",
        "total": total,
        "offset": offset,
        "limit": limit,
        "count": len(messages),
        "remaining": remaining,
        "has_more": remaining > 0,
        "messages": messages,
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
