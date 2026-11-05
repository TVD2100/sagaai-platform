"""
core.gateway.backup - consistent snapshots of the installation databases.

A file copy of a SQLite database is not a backup. In WAL mode the committed
state lives partly in ``-wal``, a copy taken while a writer runs can capture a
torn page, and restoring such a copy later fails an integrity check nobody ran
before the restore. ``sqlite3.Connection.backup`` is the primitive that solves
all three: it copies through the database engine, it produces a single
self-contained file, and it is safe while readers and writers use the source.

A snapshot is a FOLDER, not a file, because an installation has two databases
whose consistency is independent of each other:

    <root>/backups/databases/<snapshot id>/
        manifest.json      - id, time, label, version, members, sizes, sha256
        sagaai.db          - the main database (assistants, settings, ...)
        devagent.db        - the agent database (dialogs, runs index)

Restoring replaces the database FILES, which is why every caller must confirm
the intent first and why a snapshot of the CURRENT state is taken before the
replacement: a restore that goes wrong must be undoable by another restore.

The replacement is atomic per file (write a temporary file next to the target,
then ``os.replace``), and every member is integrity-checked before anything is
replaced - a corrupt snapshot is refused instead of installed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

log = logging.getLogger("core.gateway.backup")

#: Folder of database snapshots, relative to the DATA folder.
#:
#: Snapshots are runtime data, not code: they live next to the databases they
#: copy. Putting them under the installation root would mix a user's data with
#: a published file tree, and a `SAGAAI_DATA_DIR` override would then stop
#: covering the whole installation state.
BACKUPS_RELDIR = os.path.join("backups", "databases")
#: Manifest of one snapshot.
MANIFEST_NAME = "manifest.json"
#: Shape of a snapshot id. Validated on input so a caller cannot address a
#: path outside the snapshot folder with a crafted id.
SNAPSHOT_ID_PREFIX = "db_"


def backups_root(root: str = "") -> str:
    """Return the folder that holds database snapshots."""
    base = str(root or "").strip() or _data_dir()
    return os.path.join(base, BACKUPS_RELDIR)


def _install_root() -> str:
    """Return the installation root the platform runs from."""
    try:
        from core.paths import BASE_DIR
        return str(BASE_DIR)
    except Exception:  # pragma: no cover - defensive
        return os.getcwd()


def _data_dir() -> str:
    """Return the data folder that holds the databases."""
    try:
        from core.paths import DATA_DIR
        return str(DATA_DIR)
    except Exception:  # pragma: no cover - defensive
        return _install_root()


def database_paths() -> Dict[str, str]:
    """Return the databases a snapshot covers, keyed by member name.

    ``core.paths`` is read at call time so a test that overrides DATA_DIR is
    honoured, and the names are the FILE names so a restore can address a
    member the same way the manifest does.
    """
    data = _data_dir()
    return {
        "sagaai.db": os.path.join(data, "sagaai.db"),
        "devagent.db": os.path.join(data, "devagent.db"),
    }


# ─── Small helpers ──────────────────────────────────────────────────────────


def _utc_now() -> str:
    """Return the current UTC time with microsecond precision.

    Seconds are not enough: two snapshots taken in the same second differ
    only by their folder name, and a listing that claims to be newest-first
    would then be ordered by accident. The microseconds make the recorded
    time a real ordering key.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _size_of(path: str) -> int:
    try:
        return int(os.path.getsize(path))
    except OSError:
        return 0


def new_snapshot_id() -> str:
    """Return a fresh, sortable snapshot id.

    The stamp carries microseconds so ids sort in creation order; the random
    tail keeps two ids distinct if the clock does not advance between calls.
    """
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    micros = "%06d" % (int(time.time() * 1000000) % 1000000)
    return SNAPSHOT_ID_PREFIX + stamp + micros + "_" + uuid.uuid4().hex[:6]


def is_snapshot_id(value: str) -> bool:
    """Return True when *value* has the shape of a snapshot id."""
    text = str(value or "").strip()
    if not text.startswith(SNAPSHOT_ID_PREFIX):
        return False
    tail = text[len(SNAPSHOT_ID_PREFIX):]
    if not tail or len(tail) > 64:
        return False
    return all(ch.isalnum() or ch in "_-" for ch in tail)


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    """Write a JSON file atomically."""
    tmp = path + ".tmp-%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def integrity_check(path: str) -> str:
    """Return the ``PRAGMA integrity_check`` verdict of one database file.

    A snapshot that fails this check is worthless: discovering it at restore
    time turns a routine maintenance action into an outage. The check runs on
    a read-only connection, so a file that is merely unreadable reports the
    error instead of raising.
    """
    if not os.path.isfile(path):
        return "missing"
    try:
        uri = "file:%s?mode=ro" % path.replace("?", "%3f")
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except Exception as exc:
        return "error: " + str(exc)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return str((row or ["unknown"])[0])
    except Exception as exc:
        return "error: " + str(exc)
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - closing is best effort
            pass


# ─── Create ─────────────────────────────────────────────────────────────────


def _copy_database(src: str, dst: str) -> None:
    """Copy one database through the engine into a new single file."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    source = sqlite3.connect(src, timeout=10.0)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def create_backup(label: str = "", root: str = "") -> Dict[str, Any]:
    """Snapshot every existing installation database into a new folder.

    A database whose file does not exist is recorded as absent rather than
    created: a snapshot of a clean installation is a valid, useful artefact -
    it says "there was nothing here", which a restore can reproduce.

    Returns the manifest, including ``ok`` and ``error``; never raises for a
    missing database, only for a failure to write the snapshot itself.
    """
    snapshot_id = new_snapshot_id()
    target_dir = os.path.join(backups_root(root), snapshot_id)
    members: List[Dict[str, Any]] = []
    created = False
    try:
        os.makedirs(target_dir, exist_ok=True)
        created = True
        for name, src in database_paths().items():
            if not os.path.isfile(src):
                members.append({"name": name, "present": False,
                                "size": 0, "sha256": "",
                                "integrity": "missing"})
                continue
            dst = os.path.join(target_dir, name)
            _copy_database(src, dst)
            verdict = integrity_check(dst)
            if verdict != "ok":
                raise RuntimeError("snapshot of %s failed integrity check: %s"
                                   % (name, verdict))
            members.append({
                "name": name,
                "present": True,
                "size": _size_of(dst),
                "sha256": _sha256_file(dst),
                "integrity": verdict,
            })
        manifest = {
            "ok": True,
            "id": snapshot_id,
            "created_at": _utc_now(),
            "label": str(label or ""),
            "version": _platform_version(),
            "data_dir": _data_dir(),
            "members": members,
            "error": None,
        }
        _write_json(os.path.join(target_dir, MANIFEST_NAME), manifest)
        return manifest
    except Exception as exc:
        log.warning("database snapshot failed: %s", exc)
        if created:
            shutil.rmtree(target_dir, ignore_errors=True)
        return {"ok": False, "id": "", "created_at": _utc_now(),
                "label": str(label or ""), "version": _platform_version(),
                "members": [], "error": str(exc)}


def _platform_version() -> str:
    try:
        from core.version import __version__
        return str(__version__)
    except Exception:  # pragma: no cover - version is informational
        return ""


# ─── List / read ────────────────────────────────────────────────────────────


def read_manifest(snapshot_id: str, root: str = "") -> Dict[str, Any]:
    """Return the manifest of one snapshot, or an empty dict."""
    if not is_snapshot_id(snapshot_id):
        return {}
    return _read_json(os.path.join(backups_root(root), snapshot_id, MANIFEST_NAME))


def list_backups(root: str = "") -> List[Dict[str, Any]]:
    """Return snapshot manifests, newest first.

    A folder without a readable manifest is skipped: it is either a snapshot
    in progress or a leftover of a crash, and neither can be restored without
    knowing what it contains.
    """
    base = backups_root(root)
    try:
        names = sorted(os.listdir(base), reverse=True)
    except OSError:
        return []
    items: List[Dict[str, Any]] = []
    for name in names:
        if not is_snapshot_id(name):
            continue
        manifest = read_manifest(name, root)
        if not manifest:
            continue
        manifest.setdefault("id", name)
        items.append(manifest)
    # Newest first by the RECORDED time, not by the folder name: a folder name
    # is unique but its tail is random, so sorting by it orders two snapshots
    # of the same second by accident.
    items.sort(key=lambda item: (str(item.get("created_at") or ""),
                                 str(item.get("id") or "")), reverse=True)
    return items


# ─── Restore ────────────────────────────────────────────────────────────────


def _atomic_replace(src: str, dst: str) -> None:
    """Replace *dst* with *src* atomically, keeping the same filesystem.

    The database lives next to ``-wal`` and ``-shm`` files; replacing the main
    file while a stale WAL is present would attach an old log to a new
    database. Every sidecar is removed together with the replacement, which is
    why this runs with the platform stopped.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp-restore-%d" % os.getpid()
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    for suffix in ("-wal", "-shm"):
        sidecar = dst + suffix
        if os.path.exists(sidecar):
            try:
                os.remove(sidecar)
            except OSError as exc:  # pragma: no cover - best effort
                log.warning("stale sidecar %s not removed: %s", sidecar, exc)


def restore_backup(snapshot_id: str, root: str = "",
                   safety_snapshot: bool = True) -> Dict[str, Any]:
    """Restore one snapshot over the live databases.

    ``safety_snapshot=True`` takes a snapshot of the CURRENT state first: the
    caller may have restored the wrong folder, and the only way back is a
    snapshot made before the replacement. The safety snapshot is returned in
    the report so the operator sees how to undo.

    Every member is integrity-checked before anything is replaced; a single
    bad member aborts the whole restore, because replacing one database and
    keeping another from the old state is a combination no backup ever
    captured.
    """
    if not is_snapshot_id(snapshot_id):
        return {"ok": False, "restored": [], "error": "invalid snapshot id",
                "safety_snapshot": ""}
    snapshot_dir = os.path.join(backups_root(root), snapshot_id)
    manifest = _read_json(os.path.join(snapshot_dir, MANIFEST_NAME))
    if not manifest:
        return {"ok": False, "restored": [],
                "error": "snapshot not found: " + snapshot_id,
                "safety_snapshot": ""}

    members = [m for m in (manifest.get("members") or [])
               if isinstance(m, dict) and m.get("present")]
    plan: List[Dict[str, Any]] = []
    for member in members:
        name = str(member.get("name") or "")
        if os.path.basename(name) != name or not name.endswith(".db"):
            return {"ok": False, "restored": [],
                    "error": "unsafe member name in manifest: " + name,
                    "safety_snapshot": ""}
        src = os.path.join(snapshot_dir, name)
        if not os.path.isfile(src):
            return {"ok": False, "restored": [],
                    "error": "snapshot member missing: " + name,
                    "safety_snapshot": ""}
        expected = str(member.get("sha256") or "")
        if expected and _sha256_file(src) != expected:
            return {"ok": False, "restored": [],
                    "error": "snapshot member does not match its manifest: " + name,
                    "safety_snapshot": ""}
        verdict = integrity_check(src)
        if verdict != "ok":
            return {"ok": False, "restored": [],
                    "error": "snapshot member failed integrity check: %s (%s)"
                             % (name, verdict),
                    "safety_snapshot": ""}
        plan.append({"name": name, "src": src})

    safety = ""
    if safety_snapshot:
        report = create_backup(label="before restore of " + snapshot_id, root=root)
        safety = str(report.get("id") or "") if report.get("ok") else ""
        if not safety:
            return {"ok": False, "restored": [],
                    "error": "safety snapshot failed: " + str(report.get("error")),
                    "safety_snapshot": ""}

    targets = database_paths()
    restored: List[str] = []
    try:
        for item in plan:
            name = item["name"]
            dst = targets.get(name) or os.path.join(_data_dir(), name)
            _atomic_replace(item["src"], dst)
            restored.append(name)
    except Exception as exc:
        log.warning("database restore failed after %s: %s", restored, exc)
        return {"ok": False, "restored": restored,
                "error": str(exc), "safety_snapshot": safety}

    return {"ok": True, "restored": restored, "error": None,
            "safety_snapshot": safety, "snapshot_id": snapshot_id}
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
