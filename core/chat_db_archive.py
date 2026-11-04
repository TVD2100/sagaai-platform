# -*- coding: utf-8 -*-
"""core.chat_db_archive - one-time switch of assistant dialogues to the file store.

Assistant chat messages used to live only in the ``messages`` table of the
main database. They now also live in a per-thread append-only transcript file
(``dev_agent/transcript.py``, driven through ``core.thread_mirror``) - the
same store orchestrator dialogs already use - and the thread row keeps the
index over it.

Why the switch cannot replace the file
-------------------------------------
The orchestrator database held nothing but dialogues, so its switch archived
the whole file and started a new empty one. The main database is the opposite:
assistants, settings, connectors, the marketplace registry and RAG metadata all
live next to the messages, and replacing the file would destroy them. So the
switch works one table deeper:

1. the whole database is archived into a verified zip beside itself;
2. every thread's messages are written to its transcript file;
3. the thread row receives its index columns (path, counters, cost);
4. the migrated rows leave the ``messages`` table;
5. the layout is stamped in ``PRAGMA user_version``.

Step 4 is the only destructive one, and it runs only after step 1 produced an
archive that passed verification. A crash at any point leaves a database whose
remaining rows are still readable: an un-migrated thread simply has no
``transcript_path`` yet, and the reader falls back to the table exactly as it
did before the switch.

Layout stamp
------------
* ``0`` - written before the file store (a dialog keeps its messages in the
  table);
* ``1`` - current layout (dialogs are file-backed; the table holds only
  threads the reader cannot serve from a file).

A database with nothing to move - empty, or already current - is stamped in
place, so no second archive is ever written for the same state.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import zipfile
import zlib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

#: Layout version stamped into the main database header.
CHAT_DB_FORMAT_VERSION = 1

#: Name of the one-shot notice file left for the UI after an archive.
NOTICE_FILE_NAME = "chat_db_archive_notice.json"

#: How long a lock wait may take before the switch is reported as blocked.
_BUSY_TIMEOUT_SECONDS = 5.0

#: Read size for the checksum pass over a database being compared to a zip.
_CRC_CHUNK = 1024 * 1024


class LockBusy(RuntimeError):
    """Raised when another process holds a write lock on the database."""


def is_lock_error(text: str) -> bool:
    """True when *text* describes a database another connection has locked."""
    lowered = str(text or "").lower()
    return "locked" in lowered or "busy" in lowered


def _escape_uri(path: str) -> str:
    """Return a ``file:`` URI for *path* usable by sqlite3."""
    return Path(path).resolve().as_uri()


@contextmanager
def _connection(db_path: str, readonly: bool = False):
    """Open a short-lived SQLite connection and always close it."""
    if readonly:
        con = sqlite3.connect(_escape_uri(db_path) + "?mode=ro", uri=True,
                              timeout=_BUSY_TIMEOUT_SECONDS)
    else:
        con = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_SECONDS)
    try:
        con.row_factory = sqlite3.Row
        yield con
    finally:
        con.close()


@contextmanager
def _exclusive(db_path: str):
    """Hold an SQLite write lock for the duration of the block."""
    con = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_SECONDS)
    try:
        # Named access is what the migration reads rows with; without the row
        # factory every column would have to be addressed by a fragile index.
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise LockBusy(str(exc))
        yield con
    finally:
        try:
            con.rollback()
        except Exception:
            pass
        con.close()


def _table_columns(con, table: str) -> set:
    """Return the column names of *table*, or an empty set when it is absent."""
    try:
        return {str(row[1]) for row in con.execute("PRAGMA table_info(%s)" % table)}
    except Exception:
        return set()


def _count(con, sql: str, params=()) -> int:
    """Run one counting query; a missing table counts as zero."""
    try:
        row = con.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _threads_with_messages(con) -> int:
    """Return how many threads still keep their messages in the table."""
    columns = _table_columns(con, "threads")
    if not columns:
        return 0
    if "transcript_path" not in columns:
        # Written before the column existed: every thread is in the old layout.
        return _count(con, "SELECT count(*) FROM threads")
    return _count(
        con,
        "SELECT count(DISTINCT t.thread_id) FROM threads t "
        "JOIN messages m ON m.thread_id = t.thread_id "
        "WHERE t.transcript_path IS NULL OR trim(t.transcript_path) = ''",
    )


def inspect_database(db_path: str, with_messages: bool = False) -> dict:
    """Describe the layout of one main database without changing it.

    Args:
        db_path: path of the database file; a missing file is reported, not
            created.
        with_messages: also count the ``messages`` rows. Off by default
            because a start-up check should not pay for a full scan.

    Returns:
        A dict with ``path``, ``exists``, ``version``, ``threads``,
        ``threads_with_messages``, ``messages``, ``legacy`` and ``error``.
        ``legacy`` is True only when the file carries dialogues in the old
        layout; an unreadable file reports its error and is never legacy, so
        a damaged database is left alone instead of being rewritten.
    """
    report = {
        "path": db_path,
        "exists": False,
        "version": 0,
        "threads": 0,
        "threads_with_messages": 0,
        "messages": 0,
        "legacy": False,
        "error": "",
    }
    if not db_path or not os.path.isfile(db_path):
        return report
    report["exists"] = True
    try:
        with _connection(db_path, readonly=True) as con:
            report["version"] = int(
                con.execute("PRAGMA user_version").fetchone()[0] or 0
            )
            report["threads"] = _count(con, "SELECT count(*) FROM threads")
            report["threads_with_messages"] = _threads_with_messages(con)
            if with_messages:
                report["messages"] = _count(con, "SELECT count(*) FROM messages")
    except Exception as exc:
        report["error"] = str(exc)
        return report
    report["legacy"] = (
        report["version"] < CHAT_DB_FORMAT_VERSION
        and report["threads_with_messages"] > 0
    )
    return report


def default_archive_path(db_path: str) -> str:
    """Return a free archive path next to *db_path*.

    The first archive takes the plain ``<database>.zip`` name, which is what a
    user expects to find beside the database. A later legacy file (a restored
    backup, a copied installation) must not overwrite it, so every subsequent
    archive carries a timestamp.
    """
    plain = db_path + ".zip"
    if not os.path.exists(plain):
        return plain
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = "%s.%s.zip" % (db_path, stamp)
    counter = 1
    while os.path.exists(candidate):
        counter += 1
        candidate = "%s.%s_%d.zip" % (db_path, stamp, counter)
    return candidate


def _file_crc32(path: str) -> int:
    """Return the CRC-32 of a file's bytes, chunked so size does not matter."""
    checksum = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CRC_CHUNK)
            if not chunk:
                break
            checksum = zlib.crc32(chunk, checksum)
    return checksum & 0xFFFFFFFF


def existing_identical_archive(db_path: str) -> str:
    """Return an existing archive that holds exactly this database file.

    An archive made by hand (or by a previous switch whose replacement step
    failed) must not be duplicated: a second copy of a large database costs
    space for nothing. The comparison is byte-exact - the zip entry stores the
    uncompressed size and the CRC-32 of its member, and both are checked
    against the file on disk - so a stale or unrelated archive is never
    mistaken for a backup of the current data.

    Returns the archive path, or an empty string when no such archive exists.
    """
    plain = db_path + ".zip"
    if not (os.path.isfile(plain) and os.path.isfile(db_path)):
        return ""
    try:
        with zipfile.ZipFile(plain) as zf:
            info = zf.getinfo(os.path.basename(db_path))
            if int(info.file_size) != os.path.getsize(db_path):
                return ""
            if int(info.CRC) != _file_crc32(db_path):
                return ""
            if zf.testzip() is not None:
                return ""
    except Exception:
        return ""
    return plain


def _checkpoint_standalone(db_path: str) -> tuple:
    """Fold the write-ahead log into the database file before archiving.

    The checkpoint must run OUTSIDE a write transaction: SQLite refuses one
    inside BEGIN EXCLUSIVE, and a silently skipped checkpoint is exactly how
    messages that live only in the ``-wal`` file end up missing from a copy of
    the main file.

    Returns ``(ok, reason)``; ``reason`` is empty when the checkpoint
    succeeded.
    """
    try:
        con = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_SECONDS)
    except Exception as exc:
        return False, str(exc)
    try:
        row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        con.commit()
    except Exception as exc:
        return False, str(exc)
    finally:
        con.close()
    if row and int(row[0]) != 0:
        return False, "database is locked"
    return True, ""


def _verify_archive(archive_path: str, db_path: str) -> str:
    """Check that the archive holds an intact copy of the database.

    Returns an empty string when the archive is good, or a description of the
    problem. Rows are only removed after this passes: a truncated zip would
    otherwise be the only surviving copy of the migrated messages.
    """
    try:
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
            wanted = os.path.basename(db_path)
            if wanted not in names:
                return "archive does not contain %s" % wanted
            info = zf.getinfo(wanted)
            source_size = os.path.getsize(db_path)
            if int(info.file_size) != int(source_size):
                return "archived size %d differs from %d" % (info.file_size, source_size)
            broken = zf.testzip()
            if broken:
                return "archive member %s is corrupt" % broken
    except Exception as exc:
        return str(exc)
    return ""


def _archive_locked(db_path: str, archive_path: str = None) -> dict:
    """Write the database into a verified zip while the caller holds the lock.

    Separated from :func:`archive_database` because the migration step has to
    happen under the SAME lock as the copy: releasing it in between would let
    another process write a message into a table that is about to be emptied.

    Returns ``{ok, archive_path, bytes, error}``. The database file is never
    removed here - this module never replaces it, it only empties the migrated
    table.
    """
    report = {"ok": False, "archive_path": "", "bytes": 0, "error": ""}
    if not db_path or not os.path.isfile(db_path):
        report["error"] = "database file is missing"
        return report
    target = archive_path or default_archive_path(db_path)
    directory = os.path.dirname(target) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except Exception as exc:
        report["error"] = str(exc)
        return report

    tmp_path = ""
    try:
        # An empty companion is not worth a zip member: after a successful
        # checkpoint the log file still exists but holds nothing.
        members = [m for m in (db_path, db_path + "-wal", db_path + "-shm")
                   if os.path.isfile(m) and os.path.getsize(m) > 0]
        fd, tmp_path = tempfile.mkstemp(prefix=".archive-", suffix=".zip",
                                        dir=directory)
        os.close(fd)
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for member in members:
                zf.write(member, arcname=os.path.basename(member))
        problem = _verify_archive(tmp_path, db_path)
        if problem:
            report["error"] = problem
            return report
        os.replace(tmp_path, target)
        tmp_path = ""
    except Exception as exc:
        report["error"] = str(exc)
        return report
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    report["ok"] = True
    report["archive_path"] = target
    try:
        report["bytes"] = os.path.getsize(target)
    except OSError:
        report["bytes"] = 0
    return report


def archive_database(db_path: str, archive_path: str = None) -> dict:
    """Write *db_path* into a verified zip; nothing is removed.

    A live database is locked for the duration of the copy, so the archive is
    a consistent snapshot even while other connections are open.

    Returns ``{ok, archive_path, bytes, error}``. On failure the database is
    left untouched, including its ``-wal``/``-shm`` companions.
    """
    if not db_path or not os.path.isfile(db_path):
        return {"ok": False, "archive_path": "", "bytes": 0,
                "error": "database file is missing"}
    ok, reason = _checkpoint_standalone(db_path)
    if not ok:
        return {"ok": False, "archive_path": "", "bytes": 0,
                "error": "database is in use: %s" % reason}
    try:
        with _exclusive(db_path):
            return _archive_locked(db_path, archive_path)
    except LockBusy as exc:
        return {"ok": False, "archive_path": "", "bytes": 0,
                "error": "database is in use: %s" % exc}


def _notice_path(data_dir: str = None) -> str:
    """Return the path of the one-shot notice file."""
    if data_dir is None:
        from core.paths import DATA_DIR
        data_dir = DATA_DIR
    return os.path.join(str(data_dir), NOTICE_FILE_NAME)


def _write_notice(notice: dict, data_dir: str = None) -> bool:
    """Persist *notice* so the next render can show it exactly once."""
    try:
        path = _notice_path(data_dir)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(notice, fh, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def take_notice(data_dir: str = None) -> dict:
    """Return the pending migration notice once, then forget it.

    A file rather than a module global on purpose: the migration happens
    during the start-up script, and the message has to survive until a
    browser actually renders a page. Returning it only once keeps a rerun
    from repeating a finished migration notice.
    """
    try:
        path = _notice_path(data_dir)
        if not os.path.isfile(path):
            return {}
        with open(path, encoding="utf-8") as fh:
            notice = json.load(fh)
        try:
            os.remove(path)
        except OSError:
            pass
        return notice if isinstance(notice, dict) else {}
    except Exception:
        return {}


#: Index columns the switch writes into. The ORM adds the same ones at engine
#: start, but the switch runs BEFORE any connection is opened, and a database
#: written by an older build may still lack them.
_INDEX_COLUMNS = [
    ("parent_thread_id", "VARCHAR(64)"),
    ("kind", "VARCHAR(32)"),
    ("message_count", "INTEGER"),
    ("total_bytes", "INTEGER"),
    ("last_message_at", "VARCHAR(32)"),
    ("tokens_in", "INTEGER"),
    ("tokens_out", "INTEGER"),
    ("tokens_cache", "INTEGER"),
    ("transcript_path", "VARCHAR(1024)"),
    ("transcript_format", "VARCHAR(16)"),
    ("transcript_bytes", "INTEGER"),
    ("content_schema", "INTEGER"),
]


def _ensure_index_columns(con) -> None:
    """Add the nullable index columns the switch needs to write into."""
    existing = _table_columns(con, "threads")
    if not existing:
        return
    for name, decl in _INDEX_COLUMNS:
        if name not in existing:
            con.execute("ALTER TABLE threads ADD COLUMN %s %s" % (name, decl))


def _thread_messages(con, thread_id: str) -> List[Dict[str, Any]]:
    """Return one thread's stored messages in insertion order."""
    try:
        rows = con.execute(
            "SELECT id, role, content, ts, file_name, file_chars FROM messages "
            "WHERE thread_id = ? ORDER BY id", (thread_id,)).fetchall()
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append({
            "role": row["role"] or "user",
            "content": row["content"] or "",
            "ts": row["ts"] or "",
            "file_name": row["file_name"] or "",
            "file_chars": int(row["file_chars"] or 0),
        })
    return out


def _cost_of(messages: List[Dict[str, Any]]) -> tuple:
    """Sum the token cost the stored messages carry in their prefix."""
    from dev_agent.transcript import strip_event_prefix
    total_in = total_out = total_cache = 0
    for message in messages:
        _text, meta = strip_event_prefix(str(message.get("content") or ""))
        tokens = meta.get("tokens") or {}
        total_in += int(tokens.get("in", 0) or 0)
        total_out += int(tokens.get("out", 0) or 0)
        total_cache += int(tokens.get("cache", 0) or 0)
    return total_in, total_out, total_cache


def _write_transcripts(con) -> dict:
    """Write every table-backed dialogue into its transcript file.

    Nothing in the database changes here: files are written first, so a crash
    leaves the table intact and the next start simply writes the same files
    again. One thread failing does NOT stop the others from being written, but
    it does stop the destructive step - the caller only removes rows when
    every thread in the database has a file.

    Returns ``{migrated, failures, messages}``, where each entry of
    ``migrated`` carries what the index update needs.
    """
    from core import thread_mirror as mirror
    try:
        rows = con.execute(
            "SELECT thread_id, parent_thread_id, type FROM threads "
            "WHERE transcript_path IS NULL OR trim(transcript_path) = ''"
        ).fetchall()
    except Exception as exc:
        return {"migrated": [], "failures": [{"thread_id": "", "error": str(exc)}],
                "messages": 0}

    migrated: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    messages_total = 0
    for row in rows:
        tid = str(row["thread_id"] or "")
        if not tid:
            continue
        messages = _thread_messages(con, tid)
        if not messages:
            continue
        parent = str(row["parent_thread_id"] or "").strip() or None
        result = mirror.rewrite_messages(tid, messages, parent)
        if not result.get("ok"):
            failures.append({"thread_id": tid,
                             "error": str(result.get("error") or "write failed")})
            continue
        tokens_in, tokens_out, tokens_cache = _cost_of(messages)
        migrated.append({
            "thread_id": tid,
            "count": int(result.get("count") or len(messages)),
            "bytes": int(result.get("bytes") or 0),
            "path": str(result.get("path") or ""),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_cache": tokens_cache,
        })
        messages_total += len(messages)
    return {"migrated": migrated, "failures": failures,
            "messages": messages_total}


def _record_schema() -> int:
    """Return the version of the transcript record format.

    The index column ``content_schema`` describes the RECORDS inside the
    file, not the layout of the database, so a future record change can be
    read without migrating every file at once. The writer is imported
    lazily for the same reason ``core.thread_mirror`` does it, and a failed
    import falls back to the only version that exists today.
    """
    try:
        from dev_agent import transcript as T
        return int(getattr(T, "SCHEMA_VERSION", CHAT_DB_FORMAT_VERSION))
    except Exception:
        return CHAT_DB_FORMAT_VERSION


def _apply_index(con, migrated: List[Dict[str, Any]]) -> int:
    """Point the migrated threads at their files and drop the moved rows.

    One transaction for the whole set: a half-written index would let a reader
    trust a file for a thread whose rows are still the newer copy.
    """
    stamp = datetime.now().isoformat(timespec="seconds")
    schema = _record_schema()
    removed = 0
    for item in migrated:
        tid = item["thread_id"]
        con.execute(
            "UPDATE threads SET transcript_path = ?, transcript_format = ?, "
            "message_count = ?, total_bytes = ?, transcript_bytes = ?, "
            "tokens_in = ?, tokens_out = ?, tokens_cache = ?, "
            "last_message_at = ?, content_schema = ? WHERE thread_id = ?",
            (item["path"], "jsonl", int(item["count"]), int(item["bytes"]),
             int(item["bytes"]), int(item["tokens_in"]),
             int(item["tokens_out"]), int(item["tokens_cache"]),
             stamp, schema, tid),
        )
        before = _count(con, "SELECT count(*) FROM messages WHERE thread_id = ?",
                        (tid,))
        con.execute("DELETE FROM messages WHERE thread_id = ?", (tid,))
        removed += before
    return removed


def _stamp(db_path: str, version: int = CHAT_DB_FORMAT_VERSION) -> bool:
    """Write *version* into the database header, creating the file if needed."""
    try:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with _connection(db_path) as con:
            con.execute("PRAGMA user_version=%d" % int(version))
            con.commit()
        return True
    except Exception:
        return False


def ensure_database_format(db_path: str = None, archive_path: str = None,
                           data_dir: str = None) -> dict:
    """Bring the main database to the file-backed layout, once per file.

    Behaviour by state of the file:

    * missing - a new database is created and stamped;
    * current layout - nothing happens;
    * legacy layout without dialogues - stamped in place, no archive;
    * legacy layout with dialogues - archived, every dialogue written to its
      transcript, the index filled in, the moved rows dropped, then stamped.

    An existing archive that already holds this exact file is reused instead
    of writing a second copy. The function never raises, and it never removes
    a row it could not first archive and write to a file: an archive that
    fails - or a database locked by another running instance - leaves
    everything as it was.

    Returns ``{action, db_path, archive_path, threads, messages, failures,
    error}`` where ``action`` is one of ``created``, ``stamped``, ``migrated``,
    ``none`` or ``error``.
    """
    report = {"action": "none", "db_path": "", "archive_path": "",
              "threads": 0, "messages": 0, "failures": [], "error": ""}
    try:
        if db_path is None:
            from core.paths import DB_PATH
            db_path = DB_PATH
        report["db_path"] = db_path
        state = inspect_database(db_path)
        if state["error"]:
            report["action"] = "error"
            report["error"] = ("database is in use: %s" % state["error"]
                               if is_lock_error(state["error"])
                               else state["error"])
            return report
        if not state["exists"]:
            report["action"] = "created" if _stamp(db_path) else "error"
            return report
        if not state["legacy"]:
            # Current layout, or a legacy file with nothing in the old layout
            # to preserve. Stamping needs no archive and keeps a second start
            # from looking at the file again.
            if state["version"] < CHAT_DB_FORMAT_VERSION:
                report["action"] = "stamped" if _stamp(db_path) else "none"
            return report

        state = inspect_database(db_path, with_messages=True)
        ok, reason = _checkpoint_standalone(db_path)
        if not ok:
            report["action"] = "error"
            report["error"] = "database is in use: %s" % reason
            return report
        archived = {"ok": False, "archive_path": "", "bytes": 0,
                    "error": ""}
        written = {"migrated": [], "failures": [], "messages": 0}
        removed = 0
        try:
            with _exclusive(db_path) as con:
                _ensure_index_columns(con)
                reused = "" if archive_path else existing_identical_archive(db_path)
                if reused:
                    archived = {"ok": True, "archive_path": reused,
                                "bytes": os.path.getsize(reused), "error": ""}
                else:
                    archived = _archive_locked(db_path, archive_path)
                if archived["ok"]:
                    # Still under the lock: a message written between the copy
                    # and the row removal would be lost with the rows.
                    written = _write_transcripts(con)
                    if written["failures"]:
                        # One un-writable dialogue means the database is not
                        # fully migrated; removing ANY row now would leave the
                        # index claiming a completeness it does not have.
                        raise RuntimeError("transcript write failed for %d thread(s)"
                                           % len(written["failures"]))
                    removed = _apply_index(con, written["migrated"])
                    con.commit()
        except LockBusy as exc:
            report["action"] = "error"
            report["error"] = "database is in use: %s" % exc
            return report
        except Exception as exc:
            report["action"] = "error"
            report["error"] = str(exc)
            report["archive_path"] = archived.get("archive_path") or ""
            report["failures"] = written.get("failures") or []
            return report
        if not archived["ok"]:
            report["action"] = "error"
            report["error"] = archived["error"]
            return report
        if not _stamp(db_path):
            report["action"] = "error"
            report["error"] = "the migrated database could not be stamped"
            report["archive_path"] = archived["archive_path"]
            return report
        _write_notice({
            "archive_path": archived["archive_path"],
            "threads": len(written["migrated"]),
            "messages": int(removed),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }, data_dir)
        report.update({
            "action": "migrated",
            "archive_path": archived["archive_path"],
            "threads": len(written["migrated"]),
            "messages": int(removed),
        })
        return report
    except Exception as exc:  # pragma: no cover - defensive start-up path
        report["action"] = "error"
        report["error"] = str(exc)
        return report
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
