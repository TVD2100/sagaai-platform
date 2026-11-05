"""
sagaai migrate - bring the databases to the schema this build expects.

The migration steps themselves already exist and are idempotent:
``core.devagent_db_archive`` and ``core.chat_db_archive`` each move a legacy
layout forward, and importing ``storage.db`` creates the tables and adds the
missing columns. What a console session adds is the ORDER that makes the
operation safe on a real installation:

1. snapshot both databases FIRST (``core.gateway.backup``) - a migration that
   goes wrong is not fixable without the state it started from;
2. run the archive steps, then the schema step;
3. run ``PRAGMA integrity_check`` on both files and report it.

A missing database is not a failure: it means the platform was never started
here, and the migration CREATES it. That is the difference between this
command and ``doctor`` - one reports, the other builds.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List

from sagaai import exit_codes
from sagaai.commands import _common
from sagaai.errors import CliError

SUMMARY = "migrate the databases to the current schema"
DESCRIPTION = (
    "Snapshots both databases, then runs the idempotent format and schema "
    "migrations the platform runs at cold start. On a current installation "
    "nothing changes and the command reports that. A database that does not "
    "exist yet is created - running the command on a clean machine is a "
    "supported way to prepare it without opening the interface."
)


def register(parser: argparse.ArgumentParser, add_global_flags) -> None:
    """Declare the arguments of ``sagaai migrate``."""
    parser.add_argument("--yes", action="store_true", dest="assume_yes",
                        help="do not ask for confirmation")
    parser.add_argument("--no-backup", action="store_true", dest="no_backup",
                        help="skip the pre-migration snapshot (not recommended)")
    add_global_flags(parser)


def _snapshot(console) -> Dict[str, Any]:
    """Take the pre-migration snapshot and report where it went."""
    from core.gateway import backup

    report = _common.call(backup.create_backup, label="before migrate")
    if not report.get("ok"):
        raise CliError("backup_failed",
                       "the pre-migration snapshot failed: %s"
                       % report.get("error"),
                       exit_code=exit_codes.FAILURE,
                       hint="nothing was migrated")
    snapshot_id = str(report.get("id") or "")
    console.note("pre-migration snapshot: " + snapshot_id)
    return {"snapshot_id": snapshot_id,
            "path": os.path.join(backup.backups_root(), snapshot_id)}


def _migrate_databases() -> List[Dict[str, Any]]:
    """Run the archive steps and the schema step; return one row per step."""
    rows: List[Dict[str, Any]] = []
    for name, action in (
        ("devagent.db", _run_devagent_archive),
        ("sagaai.db", _run_chat_archive),
        ("schema", _run_schema),
    ):
        try:
            rows.append(action(name))
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            rows.append({"step": name, "status": "error",
                         "action": "error", "error": str(exc)})
    return rows


def _run_devagent_archive(name: str) -> Dict[str, Any]:
    """Bring ``devagent.db`` to the current file-store layout."""
    from core.devagent_db_archive import ensure_database_format
    report = ensure_database_format() or {}
    return {"step": name, "status": "ok",
            "action": str(report.get("action") or "none"),
            "threads": int(report.get("threads") or 0),
            "messages": int(report.get("messages") or 0)}


def _run_chat_archive(name: str) -> Dict[str, Any]:
    """Move the assistant dialogues of ``sagaai.db`` to their file store."""
    from core.chat_db_archive import ensure_database_format
    report = ensure_database_format() or {}
    return {"step": name, "status": "ok",
            "action": str(report.get("action") or "none"),
            "threads": int(report.get("threads") or 0),
            "messages": int(report.get("messages") or 0)}


def _run_schema(name: str) -> Dict[str, Any]:
    """Bring both databases to ``head`` of the schema history.

    The revision chain is the migration now: the twelve idempotent statements
    that used to run on every start are revisions 0001..0007, and the message
    layout is recorded as revision 0008. ``storage.migrations`` executes them
    and reports the revision each database reached.

    Returns:
        ``{step, status, action, head, databases}``; ``status`` is ``error``
        when any database stopped short of ``head``.
    """
    from storage import migrations

    report = _common.call(migrations.upgrade_installation)
    databases = list(report.get("databases") or [])
    failed = [item for item in databases if not item.get("ok")]
    return {"step": name, "status": "error" if failed else "ok",
            "action": "upgraded" if any(item.get("action") == "upgraded"
                                        for item in databases) else "current",
            "head": str(report.get("head") or ""),
            "databases": databases,
            "error": "; ".join(str(item.get("error") or "") for item in failed)}


def _rollback(snapshot: Dict[str, Any], console) -> Dict[str, Any]:
    """Restore the pre-migration snapshot after a failed schema step.

    The snapshot is the only state the migration started from, so it is the
    only honest way back: a revision that applied and a revision that failed
    cannot be separated by re-running the chain. A rollback that itself fails
    is reported, not raised - the original failure is what the operator needs
    to see first.

    Returns:
        ``{attempted, ok, snapshot_id, error}``.
    """
    result: Dict[str, Any] = {"attempted": False, "ok": False,
                              "snapshot_id": str(snapshot.get("snapshot_id") or ""),
                              "error": ""}
    if not result["snapshot_id"]:
        result["error"] = "no snapshot was taken, nothing to roll back to"
        return result
    result["attempted"] = True
    try:
        from core.gateway import backup

        report = _common.call(backup.restore_backup,
                              result["snapshot_id"], safety_snapshot=False)
        result["ok"] = bool(report.get("ok"))
        result["error"] = str(report.get("error") or "")
    except Exception as exc:  # noqa: BLE001 - reported with the failure
        result["error"] = str(exc)
    console.warn("the schema step failed: %s" %
                 ("rolled back to " + result["snapshot_id"] if result["ok"]
                  else "rollback failed: " + result["error"]))
    return result


def _integrity() -> Dict[str, Any]:
    """Check both database files after the migration."""
    from core.gateway import backup
    result: Dict[str, Any] = {}
    for name, path in backup.database_paths().items():
        verdict = backup.integrity_check(path)
        result[name] = verdict
    return result


def execute(args: argparse.Namespace, console) -> int:
    """Snapshot, migrate and verify the databases."""
    from core.paths import DB_PATH, DEVAGENT_DB_PATH

    existing = [path for path in (DB_PATH, DEVAGENT_DB_PATH) if os.path.isfile(path)]
    if existing:
        _common.confirm("Migrate the databases? A snapshot is taken first.",
                        assume_yes=bool(getattr(args, "assume_yes", False)))
    else:
        # Nothing to lose on a clean machine: creating the databases is the
        # documented way to prepare an installation without the interface.
        console.note("no database exists yet: this run creates them")

    snapshot: Dict[str, Any] = {"snapshot_id": "", "path": ""}
    if existing and not bool(getattr(args, "no_backup", False)):
        snapshot = _snapshot(console)
    elif existing:
        console.warn("the pre-migration snapshot was skipped by request")

    rows = _migrate_databases()
    rollback: Dict[str, Any] = {"attempted": False, "ok": False,
                                "snapshot_id": "", "error": ""}
    schema_failed = any(row.get("step") == "schema" and row.get("status") == "error"
                        for row in rows)
    if schema_failed:
        # The schema step is the only one that can leave a database between
        # two revisions: the archive steps either move a file completely or
        # leave it alone. Restoring the snapshot is therefore the one action
        # that returns the installation to a state this build has seen.
        rollback = _rollback(snapshot, console)
    integrity = _integrity()
    failed = [row for row in rows if row.get("status") == "error"]
    data: Dict[str, Any] = {
        "snapshot": snapshot,
        "rollback": rollback,
        "steps": rows,
        "integrity": integrity,
        "failed": [row.get("step") for row in failed],
    }
    if not console.json_mode:
        console.title("database migration")
        if snapshot.get("snapshot_id"):
            console.field("snapshot", snapshot.get("snapshot_id"))
            console.field("snapshot path", snapshot.get("path"))
        console.blank()
        for row in rows:
            console.status(str(row.get("step")), str(row.get("status")),
                           str(row.get("action")) +
                           (" (%d threads, %d messages)" % (
                               row.get("threads"), row.get("messages"))
                            if row.get("threads") or row.get("messages") else ""))
            if row.get("head"):
                console.bullet("revision: %s (head)" % row.get("head"))
        if rollback.get("attempted"):
            console.blank()
            console.title("rollback")
            console.status("snapshot", "ok" if rollback.get("ok") else "error",
                           rollback.get("snapshot_id") or rollback.get("error"))
        console.blank()
        console.title("integrity")
        for name, verdict in integrity.items():
            console.status(name, "ok" if verdict == "ok" else "error",
                           str(verdict))
    if failed:
        rolled = bool(rollback.get("ok"))
        raise CliError("migrate_failed",
                       "the migration failed for: " +
                       ", ".join(str(row.get("step")) for row in failed),
                       exit_code=exit_codes.FAILURE,
                       hint=("the databases were restored from snapshot "
                             + str(rollback.get("snapshot_id"))) if rolled
                            else (("restore the snapshot: sagaai backup restore "
                                   + str(snapshot.get("snapshot_id")))
                                  if snapshot.get("snapshot_id") else ""))
    console.emit("migrate", data, exit_code=exit_codes.OK,
                 version=_common.platform_version())
    return exit_codes.OK
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
