# -*- coding: utf-8 -*-
"""
core.devagent_db_archive - one-time switch of devagent.db to the file store.

Dialogue content used to be stored inside ``devagent.db`` itself. It now lives
in an append-only transcript file per thread (see ``dev_agent/transcript.py``),
and the database keeps only the index over those files. The two layouts cannot
share one database, and rewriting a file of several hundred megabytes in place
would be a long operation that risks the very data it is supposed to move.

The switch is therefore a replacement, not a migration: the old file is
archived next to itself and a new, empty database is created. Nothing is
destroyed - the archive holds the complete previous database, and the notice
left for the user names its path so the old dialogues stay reachable.

The layout is stamped in SQLite's own header field (``PRAGMA user_version``),
which costs four bytes to read and needs no extra table:

* ``0`` - written before the file store existed (legacy);
* ``1`` - current layout (every dialogue carries a transcript path).

A legacy file that holds no dialogue in the old layout - an empty database, or
one whose threads all carry a transcript path - is stamped in place and kept.
Only a real difference in layout justifies an archive, so an installation that
is already current is never touched and the switch cannot run twice.

Two safety rules shape the destructive part:

* the archive is verified (member present, size equal, zip test passes) before
the original is removed, and it is written through a temporary file, so a
crash leaves either the previous state or a complete archive;
* the whole copy-and-replace sequence runs under an SQLite write lock, because
a second running instance must never lose messages written while the first
one archives. A locked database aborts the switch instead of racing it.
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

#: Layout version stamped into the DevAgent database header.
DEVAGENT_DB_FORMAT_VERSION = 1

#: Name of the one-shot notice file left for the UI after an archive.
NOTICE_FILE_NAME = "devagent_db_archive_notice.json"

#: How long a lock wait may take before the switch is reported as blocked.
#: Long enough for a checkpoint of a real database, short enough that a start-up
#: never hangs on a database another process has locked for good.
_BUSY_TIMEOUT_SECONDS = 5.0

#: Read size for the checksum pass over a database being compared to a zip.
_CRC_CHUNK = 1024 * 1024


class LockBusy(RuntimeError):
    """Raised when another process holds a write lock on the database."""


def is_lock_error(text: str) -> bool:
    """True when *text* describes a database another connection has locked.

    SQLite reports the same condition differently depending on the statement
    that hit it (``database is locked``, ``database table is locked``), and
    the archive path has to name it consistently wherever it is detected.
    """
    lowered = str(text or "").lower()
    return "locked" in lowered or "busy" in lowered


def _escape_uri(path: str) -> str:
    """Return a ``file:`` URI for *path* usable by sqlite3.

    ``Path.as_uri`` percent-encodes spaces and non-ASCII characters, which a
    path under a user profile routinely contains (``Yandex.Disk``, Cyrillic
    folder names) and which a hand-built URI would break on.
    """
    return Path(path).resolve().as_uri()


@contextmanager
def _connection(db_path: str, readonly: bool = False):
    """Open a short-lived SQLite connection and always close it.

    A read-only connection is opened through a URI so that SQLite cannot
    create an empty database as a side effect of looking for one.
    """
    if readonly:
        con = sqlite3.connect(_escape_uri(db_path) + "?mode=ro", uri=True,
                              timeout=_BUSY_TIMEOUT_SECONDS)
    else:
        con = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_SECONDS)
    try:
        yield con
    finally:
        con.close()


@contextmanager
def _exclusive(db_path: str):
    """Hold an SQLite write lock for the duration of the block.

    Archiving copies the database file and the caller then deletes it. Both
    are only correct while no other process can write: a copy taken during a
    concurrent write can miss the newest messages, and deleting a file another
    instance has open would leave that instance writing into a file nobody can
    read back.

    Raises:
        LockBusy: the database is locked by another connection. The caller
            treats this as "do not switch now" rather than as an error to
            retry aggressively at start-up.
    """
    con = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_SECONDS)
    try:
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


def _count(con, table: str) -> int:
    """Return the row count of *table*, or 0 when the table does not exist."""
    try:
        row = con.execute("SELECT count(*) FROM %s" % table).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _table_columns(con, table: str) -> set:
    """Return the column names of *table*, or an empty set when it is absent."""
    try:
        return {str(row[1]) for row in con.execute("PRAGMA table_info(%s)" % table)}
    except Exception:
        return set()


def _threads_in_old_layout(con) -> int:
    """Return how many thread rows still keep their messages in the database.

    The current layout writes a transcript path into every dialogue row at
    creation, so a row without one is a row whose messages live in the
    ``messages`` table. A database written before the column existed counts
    every row as old.
    """
    columns = _table_columns(con, "threads")
    if not columns:
        return 0
    if "transcript_path" not in columns:
        return _count(con, "threads")
    try:
        row = con.execute(
            "SELECT count(*) FROM threads "
            "WHERE transcript_path IS NULL OR trim(transcript_path) = ''"
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def inspect_database(db_path: str, with_messages: bool = False) -> dict:
    """Describe the layout of one DevAgent database without changing it.

    Args:
        db_path: path of the database file; a missing file is reported, not
            created.
        with_messages: also count the ``messages`` rows. Off by default
            because the count is only needed for the user-facing notice and
            costs a scan that a start-up check should not pay for.

    Returns:
        A dict with ``path``, ``exists``, ``version``, ``threads``,
        ``threads_without_transcript``, ``messages``, ``legacy`` and ``error``.
        ``legacy`` is True only when the file carries dialogues in the old
        layout; an unreadable file reports its error and is never legacy, so a
        damaged database is left alone instead of being archived.
    """
    report = {
        "path": db_path,
        "exists": False,
        "version": 0,
        "threads": 0,
        "threads_without_transcript": 0,
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
            report["threads"] = _count(con, "threads")
            report["threads_without_transcript"] = _threads_in_old_layout(con)
            if with_messages:
                report["messages"] = _count(con, "messages")
    except Exception as exc:
        report["error"] = str(exc)
        return report
    report["legacy"] = (
        report["version"] < DEVAGENT_DB_FORMAT_VERSION
        and report["threads_without_transcript"] > 0
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
    """Return the CRC-32 of a file's bytes.

    The same checksum a zip entry stores for its member, so an existing
    archive can be compared to the file on disk without decompressing it.
    """
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

    An archive made by hand (or by a previous switch whose replacement failed)
    must not be duplicated: a second copy of a several-hundred-megabyte
    database costs space for nothing. The comparison is byte-exact - the zip
    entry stores both the uncompressed size and the CRC-32 of its member, and
    both are checked against the file on disk - so a stale or unrelated
    archive is never mistaken for a backup of the current data.

    Returns:
        The archive path, or an empty string when no such archive exists.
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
    inside ``BEGIN EXCLUSIVE``, and a silently skipped checkpoint is exactly
    how messages that live only in the ``-wal`` file end up missing from a
    copy of the main file.

    A checkpoint that cannot complete means another connection is writing:
    the caller treats that as "do not switch now" instead of archiving a
    snapshot it cannot vouch for.

    Returns:
        ``(ok, reason)``; ``reason`` is empty when the checkpoint succeeded.
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
    # (busy, log frames, checkpointed frames): a non-zero busy flag means the
    # log still holds frames that never reached the main file.
    if row and int(row[0]) != 0:
        return False, "database is locked"
    return True, ""


def _verify_archive(archive_path: str, db_path: str) -> str:
    """Check that the archive holds an intact copy of the database.

    Returns an empty string when the archive is good, or a description of the
    problem. The original file is only removed after this passes: a truncated
    zip would otherwise be the only surviving copy of the previous dialogues.
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

    Separated from :func:`archive_database` because the replacement step has to
    happen under the SAME lock as the copy: releasing it in between would let
    another process write a message into a database that is about to vanish.

    Returns:
        ``{ok, archive_path, bytes, error}``. The database is never removed
        here - producing the copy and dropping the original are two steps, and
        only the caller knows whether the copy was accepted.
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
    """Write *db_path* into a verified zip; the caller decides on removal.

    The archive is built in a temporary file inside the target directory and
    moved into place with ``os.replace``, so a crash mid-write leaves either
    the previous state or a complete archive - never half of one. The source
    file is NOT deleted here: this function only produces the copy, and
    ``ensure_database_format`` removes the original once it holds the archive.

    A live database is locked for the duration of the copy, so the archive is
    a consistent snapshot even while other connections are open.

    Returns:
        ``{ok, archive_path, bytes, error}``. On failure the database is left
        untouched, including its ``-wal``/``-shm`` companions.
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


def _remove_database_files(db_path: str) -> None:
    """Delete a database file and its write-ahead log companions."""
    for member in (db_path, db_path + "-wal", db_path + "-shm"):
        if os.path.isfile(member):
            try:
                os.remove(member)
            except OSError:
                pass


def _stamp(db_path: str, version: int = DEVAGENT_DB_FORMAT_VERSION) -> bool:
    """Write *version* into the database header, creating the file if needed."""
    try:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with _connection(db_path) as con:
            con.execute("PRAGMA user_version=%d" % int(version))
            con.commit()
        return True
    except Exception:
        return False


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
    """Return the pending archive notice once, then forget it.

    A file rather than a module global on purpose: the archive happens during
    the start-up script, and the message has to survive until a browser
    actually renders a page. Returning it only once keeps a rerun from
    repeating a finished migration notice.
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


def ensure_database_format(db_path: str = None, archive_path: str = None,
                          data_dir: str = None) -> dict:
    """Bring the DevAgent database to the current layout, once per file.

    Behaviour by state of the file:

    * missing - a new database is created and stamped;
    * current layout - nothing happens;
    * legacy layout without dialogues - stamped in place, no archive;
    * legacy layout with dialogues - archived, then replaced by a new empty
      database, and a notice is left for the user.

    An existing archive that already holds this exact file is reused instead
    of writing a second copy. The function never raises, and it never removes
    anything it could not archive first: an archive that fails - or a database
    locked by another running instance - leaves everything as it was.

    Returns:
        ``{action, db_path, archive_path, threads, messages, error}`` where
        ``action`` is one of ``created``, ``stamped``, ``archived``, ``none``
        or ``error``.
    """
    report = {"action": "none", "db_path": "", "archive_path": "",
              "threads": 0, "messages": 0, "error": ""}
    try:
        if db_path is None:
            from core.paths import DEVAGENT_DB_PATH
            db_path = DEVAGENT_DB_PATH
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
            if state["version"] < DEVAGENT_DB_FORMAT_VERSION:
                report["action"] = "stamped" if _stamp(db_path) else "none"
            return report

        state = inspect_database(db_path, with_messages=True)
        ok, reason = _checkpoint_standalone(db_path)
        if not ok:
            report["action"] = "error"
            report["error"] = "database is in use: %s" % reason
            return report
        try:
            with _exclusive(db_path):
                reused = "" if archive_path else existing_identical_archive(db_path)
                if reused:
                    archived = {"ok": True, "archive_path": reused,
                                "bytes": os.path.getsize(reused), "error": ""}
                else:
                    archived = _archive_locked(db_path, archive_path)
                if archived["ok"]:
                    # Still under the lock: a message written between the copy
                    # and the removal would be lost with the file.
                    _remove_database_files(db_path)
        except LockBusy as exc:
            report["action"] = "error"
            report["error"] = "database is in use: %s" % exc
            return report
        if not archived["ok"]:
            report["action"] = "error"
            report["error"] = archived["error"]
            return report
        if not _stamp(db_path):
            report["action"] = "error"
            report["error"] = "the archived database could not be replaced"
            report["archive_path"] = archived["archive_path"]
            return report
        _write_notice({
            "archive_path": archived["archive_path"],
            "threads": int(state["threads"]),
            "messages": int(state["messages"]),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }, data_dir)
        report.update({
            "action": "archived",
            "archive_path": archived["archive_path"],
            "threads": int(state["threads"]),
            "messages": int(state["messages"]),
        })
        return report
    except Exception as exc:  # pragma: no cover - defensive start-up path
        report["action"] = "error"
        report["error"] = str(exc)
        return report
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
