# -*- coding: utf-8 -*-
"""dev_agent.transcript - append-only message log outside SQLite.

Why a file and not a table: a transcript is written once and read rarely, it
grows without bound, and it must never slow down the write that produces it.
A row per message in SQLite makes every one of those properties worse - the
database file grows with the conversation, an insert contends with every other
writer on the same file, and the whole store has to be copied to back it up.
An append to a file has none of these problems: the cost is one write, growing
the log does not touch any other log, and a deleted thread is a deleted folder.

The store is the platform's ``history/`` tree, which already owns one folder
per dialog:

    <history>/<thread_id>/transcript.jsonl              a dialog
    <history>/<thread_id>/subagents/<child>/transcript.jsonl   its child

Keeping a child INSIDE its parent's folder is what makes the tree a tree: a
later cleanup deletes one folder and takes every descendant transcript with
it, and the parent id of a child is readable from its path without opening a
single file.

Record format: one JSON object per line, UTF-8, ending with a newline. The
line is the unit of atomicity.

Durability: a record is appended with ONE ``write`` to a file opened with
``O_APPEND``. The data reaches the kernel's page cache before the call returns,
so it survives the death of the writing process - the failure this log exists
for. ``fsync`` is deliberately not called per record: it costs milliseconds
and only protects against a machine losing power, which is a different problem
from a crashed child. A torn last line (a crash in the middle of a write) is
skipped by the reader instead of corrupting the transcript.

Bounded growth: a record larger than ``MAX_MESSAGE_BYTES`` is truncated with a
visible marker, because one pathological payload must not become the reason a
transcript cannot be read. A file larger than ``MAX_FILE_BYTES`` rotates to a
numbered segment, so no single file ever becomes slow to open.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

#: Version of the per-line record format. A reader that meets a larger number
#: knows the line was written by a newer build and can decide what to do
#: instead of failing to parse it.
SCHEMA_VERSION = 1

#: Name of the transcript file inside a thread folder.
TRANSCRIPT_NAME = "transcript.jsonl"

#: Sub-folder of a parent thread that holds its delegated children.
SUBAGENTS_DIR = "subagents"

#: A single record larger than this is truncated before it is written.
#: The measured maximum in a live installation is ~500 KB, so this is a sanity
#: bound for a pathological payload, not a limit normal traffic reaches.
MAX_MESSAGE_BYTES = 1024 * 1024

#: A transcript file rotates once it grows past this size. 8 MiB opens in a
#: few milliseconds and holds thousands of ordinary messages.
MAX_FILE_BYTES = 8 * 1024 * 1024

#: Marker appended to a record that had to be truncated.
_TRUNCATION_NOTE = "\n\n[transcript: {n} bytes truncated]"

#: Thread ids are used as folder names, so they are restricted to a safe set.
#: Anything else is refused instead of being "cleaned": a silently rewritten
#: id would write the transcript into a folder nobody can find again.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

#: A rotated segment, plain or compressed. ONE pattern serves both numbering
#: and reading: two patterns would let a compressed segment and a plain one
#: claim the same number and silently reorder the transcript.
_SEGMENT_RE = re.compile(r"^transcript\.(\d+)\.jsonl(\.gz)?$")


def _segment_number(name: str) -> int:
    """Return the number of a rotated segment file, or -1 for anything else."""
    match = _SEGMENT_RE.match(str(name or ""))
    return int(match.group(1)) if match else -1


def is_segment_name(name: str) -> bool:
    """True when *name* is a stored transcript file: active, rotated or compressed.

    Readers outside this module need this question answered, and answering it
    by matching a suffix in each caller is how a compressed segment quietly
    stops counting as a transcript in one of them.
    """
    return str(name or "") == TRANSCRIPT_NAME or _segment_number(name) >= 0

#: Serialises rotation and open/append within one process. Threads of the same
#: process write many transcripts at once; the lock is per PATH, not global,
#: so two unrelated dialogs never wait for each other.
_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _now_iso() -> str:
    """Return the current UTC time in the same shape the chat rows use."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lock_for(path: str) -> threading.RLock:
    """Return the lock that guards one transcript path."""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[path] = lock
        return lock


def history_root() -> Path:
    """Return the platform history root that holds every thread folder.

    Resolved lazily so a test can repoint ``core.paths.HISTORY_DIR`` (or set
    ``SAGAAI_DATA_DIR``) without reimporting this module, and so importing the
    writer never creates a directory as a side effect.
    """
    from core.paths import HISTORY_DIR
    return Path(HISTORY_DIR)


def _check_id(thread_id: str, what: str) -> str:
    """Validate one id used as a folder name."""
    tid = str(thread_id or "").strip()
    if not tid:
        raise ValueError(f"{what} must not be empty")
    if not _SAFE_ID.match(tid):
        raise ValueError(
            f"{what} {thread_id!r} is not a usable folder name "
            "(expected letters, digits, dot, dash or underscore)"
        )
    return tid


def thread_dir(thread_id: str,
               parent_thread_id: Optional[str] = None) -> Path:
    """Return the folder that owns one thread's transcript.

    A child lives inside its parent (``<parent>/subagents/<child>``) and a
    dialog lives directly under the history root. The parent is what makes a
    deletion and a walk of the tree work without reading a database.
    """
    tid = _check_id(thread_id, "thread_id")
    root = history_root()
    if parent_thread_id:
        pid = _check_id(parent_thread_id, "parent_thread_id")
        return root / pid / SUBAGENTS_DIR / tid
    return root / tid


def transcript_path(thread_id: str,
                    parent_thread_id: Optional[str] = None,
                    segment: int = 0) -> Path:
    """Return the path of one transcript segment (0 is the active file)."""
    folder = thread_dir(thread_id, parent_thread_id)
    if segment <= 0:
        return folder / TRANSCRIPT_NAME
    return folder / f"transcript.{int(segment)}{'.jsonl'}"


def _relative_to_history(path: Path) -> str:
    """Return *path* relative to the history root, POSIX style.

    The index row stores a RELATIVE path so the same database stays valid when
    the installation is moved or restored on another machine.
    """
    try:
        return path.relative_to(history_root()).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_record_path(stored: str) -> Optional[Path]:
    """Turn a path from the index row back into an absolute path.

    Relative paths are resolved against the history root of THIS installation;
    an absolute path is honoured as is (a store moved by hand). Returns None
    for an empty value, which marks a legacy thread with no transcript.
    """
    raw = str(stored or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = history_root() / candidate
    return candidate


def strip_event_prefix(content: str) -> tuple:
    """Split a stored message into its visible text and its render metadata.

    The message tables embed ``_events``/``tokens``/``hidden`` as a JSON prefix
    inside the content field, because the row has no columns for them. A
    transcript record has real fields, so the prefix is unpacked here once and
    the text is stored clean - no reader of a transcript has to know the
    legacy encoding existed.

    Returns ``(text, metadata)`` where metadata carries only the keys that are
    cheap and meaningful for a reader: ``tokens`` and ``hidden``. The ``_events``
    payload is dropped on purpose - it is render state for the chat feed, it
    measured at ~56% of a stored message, and it is meaningless to a reader of
    a transcript.
    """
    text = content if isinstance(content, str) else ""
    marker = "__DEVAGENT_EVENTS__"
    meta: Dict[str, Any] = {}
    if not text.startswith(marker):
        return text, meta
    newline = text.find("\n")
    if newline < 0:
        return text, meta
    raw = text[len(marker):newline]
    try:
        payload = json.loads(raw)
    except Exception:
        return text, meta
    if not isinstance(payload, dict):
        return text, meta
    tokens = payload.get("tokens") or payload.get("_tokens")
    if isinstance(tokens, dict) and tokens:
        meta["tokens"] = {
            "in": int(tokens.get("in", 0) or 0),
            "out": int(tokens.get("out", 0) or 0),
            "cache": int(tokens.get("cache", 0) or 0),
        }
    if payload.get("hidden"):
        meta["hidden"] = True
    return text[newline + 1:], meta


def build_record(message: Dict[str, Any], seq: int) -> Dict[str, Any]:
    """Build one storable record from a chat message dict.

    The record holds what a reader needs to reconstruct the message and its
    cost, and nothing that only the live chat feed uses.
    """
    content, meta = strip_event_prefix(str(message.get("content") or ""))
    record: Dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "seq": int(seq),
        "ts": str(message.get("ts") or "") or _now_iso(),
        "role": str(message.get("role") or "user"),
        "content": content,
    }
    file_name = str(message.get("file_name") or "")
    if file_name:
        record["file_name"] = file_name
    file_chars = int(message.get("file_chars") or 0)
    if file_chars:
        record["file_chars"] = file_chars
    tokens = meta.get("tokens") or message.get("_tokens")
    if isinstance(tokens, dict) and tokens:
        record["tokens"] = {
            "in": int(tokens.get("in", 0) or 0),
            "out": int(tokens.get("out", 0) or 0),
            "cache": int(tokens.get("cache", 0) or 0),
        }
    if meta.get("hidden") or message.get("hidden"):
        record["hidden"] = True
    return record


def _encode_record(record: Dict[str, Any]) -> bytes:
    """Serialise one record into a single line, truncating if oversized.

    A record that cannot be stored at all would lose the message entirely, so
    an oversized payload is cut with a visible marker: a reader sees that
    something was omitted and how much, instead of seeing nothing.
    """
    line = json.dumps(record, ensure_ascii=False)
    raw = line.encode("utf-8", errors="replace")
    if len(raw) <= MAX_MESSAGE_BYTES:
        return raw + b"\n"
    # Cut the CONTENT, never the envelope: the record must stay parseable so a
    # reader still learns the role, the time and that something was dropped.
    overflow = len(raw) - MAX_MESSAGE_BYTES
    content = str(record.get("content") or "")
    keep = max(0, len(content.encode("utf-8", errors="replace")) - overflow - 256)
    cut = content.encode("utf-8", errors="replace")[:keep].decode("utf-8", errors="ignore")
    trimmed = dict(record)
    trimmed["content"] = cut + _TRUNCATION_NOTE.format(n=overflow)
    trimmed["truncated"] = True
    return json.dumps(trimmed, ensure_ascii=False).encode("utf-8", errors="replace") + b"\n"


def _active_segment(folder: Path) -> bool:
    """Rotate the active file when it is full; True when a rotation happened.

    Rotation is a rename plus a fresh empty active file, so it costs one
    syscall and never rewrites the data it is rotating. The caller uses the
    answer to decide whether this is a cheap moment for housekeeping.
    """
    highest = 0
    try:
        for entry in folder.iterdir():
            number = _segment_number(entry.name)
            if number > highest:
                highest = number
    except OSError:
        return False
    active = folder / TRANSCRIPT_NAME
    try:
        size = active.stat().st_size
    except OSError:
        return False
    if size < MAX_FILE_BYTES:
        return False
    # The active file is full: it becomes the next numbered segment and a fresh
    # active file starts empty. Numbering continues past the highest existing
    # segment so a rotation never overwrites an older one.
    target = folder / f"transcript.{highest + 1}.jsonl"
    try:
        os.replace(active, target)
    except OSError:
        return False
    return True


def _housekeep(folder: Path, thread_id: str,
               parent_thread_id: Optional[str]) -> None:
    """Reclaim space at a rotation, when the folder is already being changed.

    Rotation happens once per ``MAX_FILE_BYTES`` of a thread, so this work is
    paid for on a handful of appends in a long conversation instead of on a
    schedule that would have to exist separately. Two bounds are applied:

    - cold rotated segments are compressed (lossless, reversible);
    - the per-thread quota drops the oldest segments if the thread is over it.

    Never raises: housekeeping runs inside a write, and a write that fails
    because of it would lose the message the caller was storing.
    """
    try:
        for path in _segments(folder):
            if path.name == TRANSCRIPT_NAME or path.name.endswith(".gz"):
                continue
            age = max(0.0, (time.time() - _mtime(path)) / 86400.0)
            if age >= COMPRESS_AFTER_DAYS:
                compress_segment(path)
        if thread_bytes(thread_id, parent_thread_id,
                        include_children=False) > MAX_THREAD_BYTES:
            enforce_quota(thread_id, parent_thread_id,
                          max_bytes=MAX_THREAD_BYTES)
    except Exception:  # pragma: no cover - housekeeping must not break a write
        return


def append_record(record: Dict[str, Any], thread_id: str,
                  parent_thread_id: Optional[str] = None) -> Dict[str, Any]:
    """Append ONE record to a thread transcript; return a write summary.

    The write is a single ``os.write`` to a file opened with ``O_APPEND`` and
    closed immediately, so a record is never interleaved with another writer's
    record and a crashed caller leaves at most one incomplete last line.

    Returns ``{ok, path, bytes, segment, error}``. A failed write never raises:
    a transcript is an aid to recovery, and losing it must not turn a working
    run into a failed one.
    """
    try:
        folder = thread_dir(thread_id, parent_thread_id)
        folder.mkdir(parents=True, exist_ok=True)
        data = _encode_record(record)
        with _lock_for(str(folder)):
            rotated = _active_segment(folder)
            if rotated:
                _housekeep(folder, thread_id, parent_thread_id)
            target = folder / TRANSCRIPT_NAME
            fd = os.open(str(target),
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                written = os.write(fd, data)
            finally:
                os.close(fd)
        return {
            "ok": True,
            "path": _relative_to_history(target),
            "bytes": int(written),
            "error": "",
        }
    except Exception as exc:  # pragma: no cover - a log must not break a run
        return {"ok": False, "path": "", "bytes": 0, "error": str(exc)}


def append_messages(thread_id: str, messages: Iterable[Dict[str, Any]],
                    parent_thread_id: Optional[str] = None,
                    start_seq: int = 1) -> Dict[str, Any]:
    """Append several messages, numbering them from *start_seq*.

    Returns ``{ok, path, count, bytes, error}``. Used when a whole turn is
    persisted at once instead of message by message.
    """
    seq = int(start_seq)
    count = 0
    total = 0
    last_path = ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        result = append_record(build_record(message, seq), thread_id,
                               parent_thread_id)
        if not result.get("ok"):
            return {"ok": False, "path": last_path, "count": count,
                    "bytes": total, "error": result.get("error", "")}
        last_path = result["path"]
        total += int(result.get("bytes") or 0)
        count += 1
        seq += 1
    return {"ok": True, "path": last_path, "count": count,
            "bytes": total, "error": ""}


def _segments(folder: Path) -> List[Path]:
    """Return the transcript segments of one thread in reading order.

    A compressed segment sorts by its NUMBER, not by its suffix: the same
    segment exists as ``.jsonl`` before compression and as ``.jsonl.gz``
    after, and it must keep its place in the sequence across that change.
    """
    out: List[Path] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return out
    numbered = []
    for entry in entries:
        number = _segment_number(entry.name)
        if number >= 0:
            numbered.append((number, entry))
    numbered.sort(key=lambda item: item[0])
    out.extend(entry for _, entry in numbered)
    active = folder / TRANSCRIPT_NAME
    if active.exists():
        out.append(active)
    return out


def iter_records(thread_id: str,
                 parent_thread_id: Optional[str] = None) -> Iterator[Dict[str, Any]]:
    """Yield every complete record of a thread, oldest first.

    An unparseable line is SKIPPED, not fatal: the last line of a crashed
    write is expected to be torn, and one bad line must not hide the rest of
    the history from the agent that is trying to recover the work.

    A compressed segment is read through :mod:`gzip` with the same parsing:
    compression is a storage detail, and a reader must never have to know
    which segments happened to be compressed on the day it ran.
    """
    folder = thread_dir(thread_id, parent_thread_id)
    for segment in _segments(folder):
        try:
            if segment.name.endswith(".gz"):
                handle = gzip.open(segment, "rt", encoding="utf-8",
                                   errors="replace")
            else:
                handle = open(segment, "r", encoding="utf-8",
                              errors="replace")
            with handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(record, dict):
                        yield record
        except OSError:
            continue


#: Role of a checkpoint record. A checkpoint is NOT a message: it never
#: enters the conversation the model reads and never appears in a window. It
#: is the platform's own progress marker - written on a step boundary, read
#: when a run has to be continued after the process that ran it died.
CHECKPOINT_ROLE = "checkpoint"

#: How many entries of each list a checkpoint carries. A checkpoint answers
#: "where is this run", not "reproduce its whole plan": lists longer than the
#: plan of a realistic task are cut, and the note keeps the rest.
MAX_CHECKPOINT_ITEMS = 40


def build_checkpoint(seq: int, *, step: int = 0, max_steps: int = 0,
                     plan: Iterable[str] = (), done: Iterable[str] = (),
                     pending: Iterable[str] = (), written: Iterable[str] = (),
                     status: str = "", note: str = "",
                     tokens: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Build a checkpoint record describing where a child stands.

    Written on a step boundary, never inside a step, so it describes a state
    that is consistent: every message up to *seq* is on disk and every path in
    *written* already exists. That is exactly what a resume needs and exactly
    what a mid-step snapshot could not promise.
    """
    cost = dict(tokens or {})
    return {
        "v": SCHEMA_VERSION,
        "seq": int(seq),
        "ts": _now_iso(),
        "role": CHECKPOINT_ROLE,
        "content": "",
        "step": int(step or 0),
        "max_steps": int(max_steps or 0),
        "plan": [str(x) for x in plan][:MAX_CHECKPOINT_ITEMS],
        "done": [str(x) for x in done][:MAX_CHECKPOINT_ITEMS],
        "pending": [str(x) for x in pending][:MAX_CHECKPOINT_ITEMS],
        "written": [str(x) for x in written][:MAX_CHECKPOINT_ITEMS],
        "status": str(status or ""),
        "note": str(note or "")[:500],
        "tokens": {"in": int(cost.get("in", 0) or 0),
                   "out": int(cost.get("out", 0) or 0),
                   "cache": int(cost.get("cache", 0) or 0)},
    }


def append_checkpoint(thread_id: str, checkpoint: Dict[str, Any], seq: int,
                      parent_thread_id: Optional[str] = None) -> Dict[str, Any]:
    """Append one checkpoint record to a transcript.

    Returns the same summary as :func:`append_record`. A checkpoint is a
    convenience for recovery, never a reason for a run to fail: a failed
    write is reported and the child carries on.
    """
    record = dict(checkpoint or {})
    record["v"] = int(record.get("v") or SCHEMA_VERSION)
    record["seq"] = int(seq)
    record.setdefault("ts", _now_iso())
    record["role"] = CHECKPOINT_ROLE
    record.setdefault("content", "")
    return append_record(record, thread_id, parent_thread_id)


def is_checkpoint(record: Dict[str, Any]) -> bool:
    """True when *record* is a progress marker rather than a message."""
    return str(record.get("role") or "") == CHECKPOINT_ROLE


def last_checkpoint(thread_id: str,
                    parent_thread_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the newest checkpoint of a thread, or None.

    Keeps the last match instead of collecting them: a long child writes
    hundreds of checkpoints and only the newest one describes the state worth
    continuing from.
    """
    latest: Optional[Dict[str, Any]] = None
    for record in iter_records(thread_id, parent_thread_id):
        if is_checkpoint(record):
            latest = record
    return latest


def exists(thread_id: str,
           parent_thread_id: Optional[str] = None) -> bool:
    """True when a thread has at least one transcript segment on disk."""
    folder = thread_dir(thread_id, parent_thread_id)
    return bool(_segments(folder))


def rewrite_records(thread_id: str, records: Iterable[Dict[str, Any]],
                    parent_thread_id: Optional[str] = None) -> Dict[str, Any]:
    """Replace a whole transcript with *records*, atomically.

    Needed when the caller rewrites a conversation instead of appending to it
    (an edit, a deletion, a regeneration). The new content is written to a
    side file and moved over the old one with a single rename, so a reader
    never sees a half-written transcript: either the previous version or the
    new one is on disk, never a mixture of the two.

    Returns ``{ok, path, count, bytes, error}``.
    """
    try:
        folder = thread_dir(thread_id, parent_thread_id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / TRANSCRIPT_NAME
        temp = folder / (TRANSCRIPT_NAME + ".tmp")
        count = 0
        total = 0
        with _lock_for(str(folder)):
            with open(temp, "wb") as handle:
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    data = _encode_record(record)
                    handle.write(data)
                    total += len(data)
                    count += 1
            os.replace(temp, target)
        return {"ok": True, "path": _relative_to_history(target),
                "count": count, "bytes": total, "error": ""}
    except Exception as exc:  # pragma: no cover - a log must not break a run
        return {"ok": False, "path": "", "count": 0, "bytes": 0,
                "error": str(exc)}


def delete_transcript(thread_id: str,
                      parent_thread_id: Optional[str] = None) -> bool:
    """Remove a thread's transcript folder; True when something was removed.

    Deleting a child also deletes its own children: the folder tree is the
    ownership tree, so a removed parent can never leave an orphan transcript
    behind that nothing points at any more.
    """
    import shutil
    folder = thread_dir(thread_id, parent_thread_id)
    if not folder.exists():
        return False
    try:
        shutil.rmtree(folder)
        return True
    except OSError:
        return False


# ─── retention: quotas, compression, expiry, orphans ───────────────────────
#
# A transcript store grows forever unless something bounds it. Three separate
# bounds, because they answer three different questions:
#
# - a PER-THREAD quota answers "one runaway child must not fill the disk";
# - COMPRESSION answers "an old conversation is worth keeping, but not at
#   full size";
# - EXPIRY answers "a conversation nobody opened for a year is not worth
#   keeping at all".
#
# All three treat a rotated segment as the unit: it is deleted or compressed
# whole, oldest first, and the ACTIVE file is never touched. A partial cut
# inside the newest segment would destroy exactly the records a resume is
# about to read, and compressing a file that is open for appends would lose
# every write that lands during the copy.

#: Hard ceiling on one thread's transcript, children included. 512 MiB is
#: ~100x the largest dialog measured on a live installation, so it is a
#: runaway guard, not a limit ordinary work meets.
MAX_THREAD_BYTES = 512 * 1024 * 1024

#: A rotated segment older than this is compressed on the next sweep.
COMPRESS_AFTER_DAYS = 3

#: A transcript nobody wrote to for this long is eligible for expiry.
EXPIRE_AFTER_DAYS = 90


def _mtime(path: Path) -> float:
    """Return the modification time of *path*, or 0 when it is gone."""
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


def _transcript_files(folder: Path) -> List[Path]:
    """Return every stored segment of a folder, the active file included."""
    out: List[Path] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return out
    for entry in entries:
        if is_segment_name(entry.name):
            out.append(entry)
    return out


def _total_bytes(paths: Iterable[Path]) -> int:
    """Sum the sizes of *paths*, ignoring the ones that vanished."""
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _child_folders(folder: Path) -> List[Path]:
    """Return the transcript folders of a thread's direct children."""
    base = folder / SUBAGENTS_DIR
    try:
        return [entry for entry in base.iterdir() if entry.is_dir()]
    except OSError:
        return []


def _dialog_folders(root: Path) -> List[Path]:
    """Return the thread folders directly under a history root."""
    try:
        return [entry for entry in root.iterdir()
                if entry.is_dir() and _SAFE_ID.match(entry.name)]
    except OSError:
        return []


def thread_bytes(thread_id: str, parent_thread_id: Optional[str] = None,
                 include_children: bool = True) -> int:
    """Return the bytes one thread owns on disk.

    Children count against the dialog that spawned them: from the disk's
    point of view a dialog and its delegated children are one piece of work,
    and a quota that ignored them would let one dialog fill the volume with
    subagent transcripts alone.
    """
    folder = thread_dir(thread_id, parent_thread_id)
    total = _total_bytes(_transcript_files(folder))
    if include_children:
        for child in _child_folders(folder):
            total += _total_bytes(_transcript_files(child))
    return total


def compress_segment(path: Path) -> Dict[str, Any]:
    """Replace one rotated segment with its gzip twin, atomically.

    The compressed file is written under a temporary name and moved into
    place BEFORE the original is removed, so a crash at any point leaves
    either the plain segment or the compressed one - never a truncated pair
    and never neither.

    Returns ``{ok, path, bytes_before, bytes_after, skipped, error}``.
    """
    source = Path(path)
    if source.name.endswith(".gz"):
        return {"ok": True, "path": str(source), "bytes_before": 0,
                "bytes_after": 0, "skipped": "already compressed", "error": ""}
    target = source.with_name(source.name + ".gz")
    temp = source.with_name(source.name + ".gz.tmp")
    before = 0
    try:
        before = source.stat().st_size
        with open(source, "rb") as src, gzip.open(temp, "wb", compresslevel=6) as dst:
            # Chunked on purpose: a segment is bounded, but the copy must not
            # depend on that being true forever.
            while True:
                chunk = src.read(1024 * 256)
                if not chunk:
                    break
                dst.write(chunk)
        os.replace(temp, target)
        # Compression is not activity: the segment keeps the time of its last
        # real write, or a compressed thread would look freshly used on every
        # sweep and never age out of the retention window.
        stamp = _mtime(source)
        if stamp:
            try:
                os.utime(target, (stamp, stamp))
            except OSError:
                pass
    except Exception as exc:
        try:
            temp.unlink()
        except OSError:
            pass
        return {"ok": False, "path": str(source), "bytes_before": 0,
                "bytes_after": 0, "skipped": "", "error": str(exc)}
    try:
        source.unlink()
    except OSError:
        # The .gz is in place and supersedes the plain twin, so a failed unlink
        # wastes space but loses nothing; the next sweep retries it.
        pass
    after = 0
    try:
        after = target.stat().st_size
    except OSError:
        pass
    return {"ok": True, "path": str(target), "bytes_before": before,
            "bytes_after": after, "skipped": "", "error": ""}


def compress_transcript(thread_id: str,
                        parent_thread_id: Optional[str] = None,
                        older_than_days: float = COMPRESS_AFTER_DAYS,
                        now: Optional[float] = None) -> Dict[str, Any]:
    """Compress the rotated segments of a thread that are old enough.

    Returns ``{ok, thread_id, compressed, bytes_before, bytes_after,
    failures}``.
    """
    folder = thread_dir(thread_id, parent_thread_id)
    current = time.time() if now is None else float(now)
    compressed = 0
    before = 0
    after = 0
    failures: List[str] = []
    for path in _segments(folder):
        if path.name == TRANSCRIPT_NAME or path.name.endswith(".gz"):
            continue
        age = max(0.0, (current - _mtime(path)) / 86400.0)
        if age < float(older_than_days):
            continue
        result = compress_segment(path)
        if result.get("ok"):
            compressed += 1
            before += int(result.get("bytes_before") or 0)
            after += int(result.get("bytes_after") or 0)
        else:
            failures.append(str(result.get("error") or "compress failed"))
    return {"ok": not failures, "thread_id": thread_id,
            "compressed": compressed, "bytes_before": before,
            "bytes_after": after, "failures": failures}


def enforce_quota(thread_id: str,
                  parent_thread_id: Optional[str] = None,
                  max_bytes: int = MAX_THREAD_BYTES) -> Dict[str, Any]:
    """Drop the oldest rotated segments until a thread fits its quota.

    Whole segments only, oldest first, never the active file: the newest
    records are the ones a resume reads, and deleting an older segment keeps
    every index already handed out valid, because nothing is renumbered.

    A thread whose ACTIVE file alone exceeds the limit cannot be brought under
    it this way. That is reported rather than hidden: ``bytes_after`` stays
    above ``max_bytes`` and the caller can see that the only remaining option
    is to cut records a reader still needs.

    Returns ``{ok, thread_id, bytes_before, bytes_after, dropped, error}``.
    """
    folder = thread_dir(thread_id, parent_thread_id)
    limit = max(0, int(max_bytes))
    before = thread_bytes(thread_id, parent_thread_id, include_children=False)
    remaining = before
    dropped: List[str] = []
    if limit and before > limit:
        for path in _segments(folder):
            if remaining <= limit:
                break
            if path.name == TRANSCRIPT_NAME:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            try:
                path.unlink()
            except OSError as exc:
                return {"ok": False, "thread_id": thread_id,
                        "bytes_before": before,
                        "bytes_after": thread_bytes(thread_id, parent_thread_id,
                                                    include_children=False),
                        "dropped": dropped, "error": str(exc)}
            dropped.append(path.name)
            remaining -= size
    return {"ok": True, "thread_id": thread_id, "bytes_before": before,
            "bytes_after": thread_bytes(thread_id, parent_thread_id,
                                        include_children=False),
            "dropped": dropped, "error": ""}


def find_orphans(root: Optional[Path] = None,
                 is_known=None) -> List[Dict[str, Any]]:
    """Return child transcript folders that no index row points at.

    A reader starts from a run id, a run resolves to its row, and the row
    names the transcript - so a child folder without a row cannot be reached
    by any reader at all. Those folders appear when a process died between
    creating the folder and inserting the row, and they are the only thing
    this function reports.

    A dialog folder without a row is deliberately NOT reported: a legacy
    dialog can hold an uploaded file before it holds any transcript, and
    treating "no row" as "garbage" there would delete user data.

    ``is_known`` is an injection seam: it takes a thread id and says whether
    a row exists. It must answer True when it cannot tell - an unreadable
    store must never make every transcript look deletable.
    """
    base = Path(root) if root is not None else history_root()
    checker = is_known
    if checker is None:
        def checker(tid: str) -> bool:
            try:
                from storage.repository_devagent import (
                    repo_devagent_load_thread_meta,
                )
                return bool(repo_devagent_load_thread_meta(tid))
            except Exception:  # pragma: no cover - unknown means "keep"
                return True
    out: List[Dict[str, Any]] = []
    for dialog in _dialog_folders(base):
        for child in _child_folders(dialog):
            try:
                known = bool(checker(child.name))
            except Exception:
                known = True
            if known:
                continue
            out.append({"thread_id": child.name,
                        "parent_thread_id": dialog.name,
                        "path": child.as_posix(),
                        "bytes": _total_bytes(_transcript_files(child))})
    return out


def _folder_bytes(folder: Path) -> int:
    """Return the transcript bytes of one folder, its children included."""
    total = _total_bytes(_transcript_files(folder))
    for child in _child_folders(folder):
        total += _total_bytes(_transcript_files(child))
    return total


def _folder_newest(folder: Path) -> float:
    """Return the newest modification time among a folder's transcripts."""
    newest = 0.0
    for path in _transcript_files(folder):
        newest = max(newest, _mtime(path))
    for child in _child_folders(folder):
        for path in _transcript_files(child):
            newest = max(newest, _mtime(path))
    return newest


def _row_checker(is_known=None):
    """Return a callable that answers whether a thread has an index row."""
    if is_known is not None:
        return is_known

    def checker(tid: str) -> bool:
        try:
            from storage.repository_devagent import repo_devagent_load_thread_meta
            return bool(repo_devagent_load_thread_meta(tid))
        except Exception:  # pragma: no cover - unknown means "keep"
            return True

    return checker


def sweep(root: Optional[Path] = None, *,
          older_than_days: float = EXPIRE_AFTER_DAYS,
          max_thread_bytes: int = MAX_THREAD_BYTES,
          compress: bool = True,
          purge_orphans: bool = False,
          dry_run: bool = True,
          now: Optional[float] = None,
          is_known=None) -> Dict[str, Any]:
    """Apply the retention policy to a whole history tree.

    Expiry is deliberately narrower than "this file is old": a transcript is
    deleted only when its thread ALSO has no index row. A dialog the platform
    still lists is a dialog the user can still open, and expiring it because
    it went quiet would delete exactly the history the transcript exists to
    preserve. Quiet threads are compressed instead; compression is lossless
    and is where the space of an old conversation is actually reclaimed.

    ``dry_run`` defaults to True: this is the one function in the module that
    removes a conversation, so a caller has to ask for it explicitly, and the
    same call with ``dry_run=True`` reports exactly what it would remove.

    ``purge_orphans`` deletes the child folders that :func:`find_orphans`
    reports - unreachable by construction. It stays off unless asked for,
    because a row that is missing only temporarily (a restore in progress)
    would make a live transcript look like garbage.

    Returns ``{ok, dry_run, expired, orphans, compressed, bytes_before,
    bytes_after, errors}``.
    """
    base = Path(root) if root is not None else history_root()
    current = time.time() if now is None else float(now)
    checker = _row_checker(is_known)
    expired: List[Dict[str, Any]] = []
    compressed: List[Dict[str, Any]] = []
    errors: List[str] = []
    bytes_before = 0
    bytes_after = 0

    for dialog in _dialog_folders(base):
        size = _folder_bytes(dialog)
        bytes_before += size
        newest = _folder_newest(dialog)
        if not newest:
            continue
        age = max(0.0, (current - newest) / 86400.0)
        try:
            row_exists = bool(checker(dialog.name))
        except Exception:
            row_exists = True
        if older_than_days and age >= float(older_than_days) and not row_exists:
            expired.append({"thread_id": dialog.name, "bytes": size,
                            "age_days": round(age, 1)})
            if not dry_run:
                delete_transcript(dialog.name)
            continue
        if compress:
            for folder in [dialog] + _child_folders(dialog):
                for path in _transcript_files(folder):
                    if path.name == TRANSCRIPT_NAME or path.name.endswith(".gz"):
                        continue
                    if (current - _mtime(path)) / 86400.0 < COMPRESS_AFTER_DAYS:
                        continue
                    if dry_run:
                        compressed.append({"path": path.as_posix(),
                                           "bytes": _total_bytes([path])})
                        continue
                    result = compress_segment(path)
                    if result.get("ok"):
                        compressed.append({"path": result["path"],
                                           "bytes": int(result.get("bytes_after") or 0)})
                    else:
                        errors.append(str(result.get("error") or "compress failed"))
        if max_thread_bytes and size > int(max_thread_bytes) and not dry_run:
            enforce_quota(dialog.name, max_bytes=int(max_thread_bytes))
            for child in _child_folders(dialog):
                enforce_quota(child.name, parent_thread_id=dialog.name,
                              max_bytes=int(max_thread_bytes))
        bytes_after += _folder_bytes(dialog)

    orphans = find_orphans(base, is_known=checker)
    if orphans and purge_orphans and not dry_run:
        for orphan in orphans:
            try:
                import shutil
                shutil.rmtree(str(orphan.get("path") or ""), ignore_errors=True)
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(str(exc))

    return {"ok": not errors, "dry_run": bool(dry_run),
            "expired": expired, "orphans": orphans,
            "compressed": compressed, "bytes_before": bytes_before,
            "bytes_after": bytes_after, "errors": errors}


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0