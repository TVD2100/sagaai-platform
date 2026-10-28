"""
core.marketplace.store - filesystem layout, locking and atomic writes.

Every on-disk artefact of the marketplace lives under ``DATA_DIR/.marketplace``:

    staging/<install_id>/        unpacked package before it is committed
    backup/<install_id>/         copies of files replaced by a run
    cache/packages/<sha256>.tar.gz
    cache/index.json             package cache index (TTL + LRU)
    registry.json                exported copy of the SQLite registry
    removed.json                 exported copy of the removal markers
    state.json                   run currently in flight (for recovery)
    history.json                 log of install/update/uninstall runs
    install.lock                 inter-process install mutex

The registry/removed exports are caches: the source of truth is the SQLite
tables (see ``core.marketplace.registry``).

Two seams exist deliberately so tests can inject failures without patching
``os.replace`` or ``time.time`` globally:

  * ``_atomic_replace(src, dst)`` - every rename goes through it
  * ``_now()`` - every timestamp goes through it
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


class MarketplaceBusyError(RuntimeError):
    """Raised when another install run already holds the marketplace lock."""


class MarketplaceError(RuntimeError):
    """Base error for marketplace operations carrying a machine-readable code."""

    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_result(self) -> dict:
        """Render the error as a structured ``{ok: false, ...}`` result."""
        return {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
            "details": self.details,
        }


MARKETPLACE_DIR_NAME = ".marketplace"

CACHE_TTL_DAYS = 30
CACHE_MAX_BYTES = 500 * 1024 * 1024

# Retention of run history per entity type (architecture section 5.4).
HISTORY_KEEP_BY_TYPE = {"agent": 20, "skill": 20, "rag": 3, "mcp": 20}
DEFAULT_HISTORY_KEEP = 20


# ─── Paths ────────────────────────────────────────────────────────────────

def data_dir() -> str:
    """Return the live ``DATA_DIR`` from ``core.paths``.

    The module object is looked up in ``sys.modules`` on every call so that
    ``importlib.reload(core.paths)`` in tests is respected even after this
    module has been imported.
    """
    mod = sys.modules.get("core.paths")
    if mod is None:
        import core.paths as _cp
        return _cp.DATA_DIR
    return mod.DATA_DIR


def marketplace_root() -> str:
    """Return the marketplace root directory (``DATA_DIR/.marketplace``)."""
    return os.path.join(data_dir(), MARKETPLACE_DIR_NAME)


def _sub(*parts: str) -> str:
    """Return a path inside the marketplace root."""
    return os.path.join(marketplace_root(), *parts)


def staging_dir(install_id: str = "") -> str:
    """Return the staging root, or one run's staging folder when given an id."""
    base = _sub("staging")
    return os.path.join(base, install_id) if install_id else base


def backup_dir(install_id: str = "") -> str:
    """Return the backup root, or one run's backup folder when given an id."""
    base = _sub("backup")
    return os.path.join(base, install_id) if install_id else base


def packages_cache_dir() -> str:
    """Return the package cache directory."""
    return _sub("cache", "packages")


def cache_index_path() -> str:
    """Return the package cache index path."""
    return _sub("cache", "index.json")


def catalog_index_cache_path() -> str:
    """Return the cached copy of the catalog index.json."""
    return _sub("cache", "catalog-index.json")


def registry_cache_path() -> str:
    """Return the exported registry cache path."""
    return _sub("registry.json")


def removed_cache_path() -> str:
    """Return the exported removal-markers cache path."""
    return _sub("removed.json")


def state_path() -> str:
    """Return the in-flight run state path."""
    return _sub("state.json")


def history_path() -> str:
    """Return the run-history path."""
    return _sub("history.json")


def lock_path() -> str:
    """Return the install mutex file path."""
    return _sub("install.lock")


def manifest_cache_dir() -> str:
    """Return the directory holding cached entity manifests."""
    return _sub("cache", "entities")


def ensure_layout() -> str:
    """Create the marketplace directory tree and return its root."""
    for path in (
        marketplace_root(),
        staging_dir(),
        backup_dir(),
        packages_cache_dir(),
        manifest_cache_dir(),
        _sub("cache"),
    ):
        os.makedirs(path, exist_ok=True)
    return marketplace_root()


# ─── Time and atomic writes ───────────────────────────────────────────────

def _now() -> datetime:
    """Return the current UTC time.

    Every timestamp the marketplace persists goes through this seam so that
    tests can freeze or shift time with a single ``monkeypatch``.
    """
    return datetime.now(timezone.utc)


def isoformat_utc(moment: Optional[datetime] = None) -> str:
    """Format *moment* (default: now) as ``YYYY-MM-DDTHH:MM:SSZ``."""
    value = moment or _now()
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_isoformat(text: str) -> Optional[datetime]:
    """Parse a timestamp produced by :func:`isoformat_utc`, else return None."""
    if not text:
        return None
    raw = str(text).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _atomic_replace(src: str, dst: str) -> None:
    """Replace *dst* with *src* atomically.

    Thin seam over ``os.replace``; tests patch this symbol to simulate a
    failure in the middle of a write without touching global ``os`` state.
    """
    os.replace(src, dst)


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Write *data* to *path* through a temporary file and an atomic rename."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".tmp-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _atomic_replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def atomic_write_json(path: str, data: Any) -> None:
    """Serialise *data* as UTF-8 JSON and write it atomically."""
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write_bytes(path, payload)


def read_json(path: str, default: Any = None) -> Any:
    """Read JSON from *path*, returning *default* on a missing/corrupt file."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def new_install_id() -> str:
    """Return a fresh run identifier: ``<YYYYMMDD>T<HHMMSS>Z-<4hex>``."""
    stamp = (_now()).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:4]}"


# ─── Install mutex ────────────────────────────────────────────────────────

_owner_guard = threading.Lock()
_owner_thread: Optional[int] = None


def _acquire_file_lock(handle) -> bool:
    """Try to take an exclusive lock on an open file handle.

    POSIX uses ``flock``; Windows uses ``msvcrt.locking`` on a single byte
    after seeking to the start. Returns False when the lock is held
    elsewhere instead of blocking.
    """
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_file_lock(handle) -> None:
    """Release a lock taken by :func:`_acquire_file_lock`."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def install_lock():
    """Serialise install runs across threads and processes.

    A file lock alone is not enough: two threads of one process can open the
    lock file with different descriptors and both win the ``flock``. The
    in-process owner flag therefore guards thread contention and the file
    lock guards other processes. Raises :class:`MarketplaceBusyError` when
    the mutex is already held instead of blocking.
    """
    global _owner_thread
    current = threading.get_ident()
    with _owner_guard:
        if _owner_thread is not None:
            raise MarketplaceBusyError(
                "Another marketplace run is already in progress"
            )
        _owner_thread = current

    handle = None
    try:
        ensure_layout()
        handle = open(lock_path(), "a+b")
        if not _acquire_file_lock(handle):
            raise MarketplaceBusyError(
                "Another process holds the marketplace install lock"
            )
        yield
    finally:
        if handle is not None:
            _release_file_lock(handle)
            handle.close()
        with _owner_guard:
            _owner_thread = None


def is_locked() -> bool:
    """True when this process currently holds the install mutex."""
    with _owner_guard:
        return _owner_thread is not None


# ─── Package cache ────────────────────────────────────────────────────────

def _load_cache_index() -> Dict[str, dict]:
    """Return the package cache index ``{sha256: entry}``."""
    data = read_json(cache_index_path(), default={})
    if not isinstance(data, dict):
        return {}
    entries = data.get("entries")
    if isinstance(entries, dict):
        return {str(k): v for k, v in entries.items() if isinstance(v, dict)}
    return {}


def _save_cache_index(entries: Dict[str, dict]) -> None:
    """Persist the package cache index."""
    atomic_write_json(cache_index_path(), {"entries": entries})


def cache_package(sha256: str, data: bytes) -> str:
    """Store a package in the cache and return its path.

    ``pinned`` entries are never evicted by the LRU sweep; the flag is set
    by callers that keep a package as a restore reference (for example the
    "replace with catalog version" conflict path).
    """
    ensure_layout()
    path = os.path.join(packages_cache_dir(), f"{sha256}.tar.gz")
    atomic_write_bytes(path, data)
    entries = _load_cache_index()
    previous = entries.get(sha256, {})
    entries[sha256] = {
        "size": len(data),
        "cached_at": isoformat_utc(),
        "pinned": bool(previous.get("pinned")),
    }
    _save_cache_index(entries)
    return path


def get_cached_package(sha256: str) -> Optional[bytes]:
    """Return cached package bytes, or None when absent or expired."""
    if not sha256:
        return None
    path = os.path.join(packages_cache_dir(), f"{sha256}.tar.gz")
    if not os.path.isfile(path):
        return None
    entries = _load_cache_index()
    entry = entries.get(sha256) or {}
    cached_at = parse_isoformat(str(entry.get("cached_at") or ""))
    if cached_at is not None and _now() - cached_at > timedelta(days=CACHE_TTL_DAYS):
        if not entry.get("pinned"):
            remove_cached_package(sha256)
            return None
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def pin_package(sha256: str) -> None:
    """Mark a cached package as pinned so the LRU sweep keeps it."""
    entries = _load_cache_index()
    entry = entries.get(sha256)
    if entry is None:
        return
    entry["pinned"] = True
    entries[sha256] = entry
    _save_cache_index(entries)


def remove_cached_package(sha256: str) -> None:
    """Delete one package from the cache and its index entry."""
    path = os.path.join(packages_cache_dir(), f"{sha256}.tar.gz")
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
    entries = _load_cache_index()
    if entries.pop(sha256, None) is not None:
        _save_cache_index(entries)


def prune_cache(max_bytes: int = CACHE_MAX_BYTES) -> List[str]:
    """Evict least-recently-cached packages until the cache fits the limit.

    Pinned entries are skipped. Returns the list of evicted sha256 keys.
    """
    entries = _load_cache_index()
    total = sum(int(entry.get("size") or 0) for entry in entries.values())
    if total <= max_bytes:
        return []
    ordered = sorted(
        entries.items(),
        key=lambda item: str(item[1].get("cached_at") or ""),
    )
    evicted: List[str] = []
    for sha, entry in ordered:
        if total <= max_bytes:
            break
        if entry.get("pinned"):
            continue
        total -= int(entry.get("size") or 0)
        evicted.append(sha)
        remove_cached_package(sha)
    return evicted


# ─── Run history and state ────────────────────────────────────────────────

def load_history() -> List[dict]:
    """Return the run log, newest last."""
    data = read_json(history_path(), default=[])
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def save_history(records: Iterable[dict]) -> None:
    """Persist the run log, applying per-type retention first."""
    atomic_write_json(history_path(), prune_history(list(records)))


def prune_history(records: List[dict], keep: Optional[Dict[str, int]] = None) -> List[dict]:
    """Trim the run log so every type keeps at most its allowance.

    The newest runs of each type are kept; the result preserves the original
    chronological order. Records without a type are treated as ``skill``.
    """
    limits = dict(HISTORY_KEEP_BY_TYPE)
    if keep:
        limits.update(keep)
    keep_flags: List[bool] = [False] * len(records)
    counters: Dict[str, int] = {}
    for index in range(len(records) - 1, -1, -1):
        type_ = str(records[index].get("type") or "skill")
        limit = int(limits.get(type_, DEFAULT_HISTORY_KEEP))
        seen = counters.get(type_, 0)
        if seen < limit:
            keep_flags[index] = True
            counters[type_] = seen + 1
    return [record for record, keep_it in zip(records, keep_flags) if keep_it]


def append_run(record: dict) -> dict:
    """Append one run record and return it with its generated ``install_id``.

    The caller may pass ``install_id``; otherwise a new one is generated.
    ``started_at`` defaults to the current time.
    """
    run = dict(record)
    run.setdefault("install_id", new_install_id())
    run.setdefault("started_at", isoformat_utc())
    run.setdefault("status", "applying")
    records = load_history()
    records.append(run)
    save_history(records)
    set_state(run["install_id"], run)
    return run


def update_run(install_id: str, **fields: Any) -> Optional[dict]:
    """Patch one run record by id and return it, or None when unknown."""
    records = load_history()
    updated = None
    for record in records:
        if record.get("install_id") == install_id:
            record.update(fields)
            updated = record
            break
    if updated is not None:
        save_history(records)
    return updated


def find_run(install_id: str) -> Optional[dict]:
    """Return one run record by id, or None."""
    for record in load_history():
        if record.get("install_id") == install_id:
            return record
    return None


def set_state(install_id: str, payload: Optional[dict]) -> None:
    """Record the run currently in flight (``payload=None`` clears it)."""
    if payload is None:
        if os.path.isfile(state_path()):
            try:
                os.remove(state_path())
            except OSError:
                pass
        return
    atomic_write_json(state_path(), {"install_id": install_id, **payload})


def get_state() -> Optional[dict]:
    """Return the in-flight run payload, or None when no run is active."""
    data = read_json(state_path(), default=None)
    return data if isinstance(data, dict) and data.get("install_id") else None


def clear_state() -> None:
    """Forget the in-flight run."""
    set_state("", None)


def applying_runs() -> List[dict]:
    """Return every run still marked ``applying`` (candidates for recovery)."""
    return [record for record in load_history() if record.get("status") == "applying"]


# ─── Scratch directories ──────────────────────────────────────────────────

def prepare_run_dirs(install_id: str) -> Tuple[str, str]:
    """Create and return ``(staging, backup)`` folders for one run."""
    stage = staging_dir(install_id)
    backup = backup_dir(install_id)
    shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)
    os.makedirs(backup, exist_ok=True)
    return stage, backup


def cleanup_run_dirs(install_id: str, *, keep_backup: bool = True) -> None:
    """Remove a run's staging folder and, unless asked, its backup folder."""
    shutil.rmtree(staging_dir(install_id), ignore_errors=True)
    if not keep_backup:
        shutil.rmtree(backup_dir(install_id), ignore_errors=True)


def copy_into_backup(src: str, install_id: str, rel_path: str) -> str:
    """Copy *src* into the run backup under *rel_path* and return the copy.

    Used before overwriting or deleting a file so a rollback can restore it
    byte-for-byte. Missing sources are ignored (nothing to preserve).
    """
    target = os.path.join(backup_dir(install_id), "files", rel_path)
    if not os.path.exists(src):
        return target
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.isdir(src):
        shutil.copytree(src, target, dirs_exist_ok=True)
    else:
        shutil.copy2(src, target)
    return target


def write_run_meta(install_id: str, meta: dict) -> str:
    """Write ``.run-meta.json`` into the run backup and return its path."""
    path = os.path.join(backup_dir(install_id), ".run-meta.json")
    atomic_write_json(path, meta)
    return path


def read_run_meta(install_id: str) -> Optional[dict]:
    """Read ``.run-meta.json`` of a run, or None when absent."""
    data = read_json(os.path.join(backup_dir(install_id), ".run-meta.json"), default=None)
    return data if isinstance(data, dict) else None
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
