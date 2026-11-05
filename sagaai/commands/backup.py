"""
sagaai backup - snapshots of the installation databases.

The heavy lifting is in ``core.gateway.backup``: it copies through the SQLite
engine (a plain file copy of a WAL database is not a backup), records a
manifest with a sha256 per member, and integrity-checks every member before a
restore replaces anything. This command adds the console rules:

- ``create`` and ``list`` change nothing that runs, so they need no
  confirmation;
- ``restore`` replaces the live files, so it asks first, refuses while the
  platform is running (exit 3) and takes a safety snapshot of the CURRENT
  state before it touches anything - a restore of the wrong snapshot is then
  undone by restoring the safety one.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict

from sagaai import exit_codes
from sagaai.commands import _common
from sagaai.console import human_bytes
from sagaai.errors import config_error

SUMMARY = "snapshot and restore the installation databases"
DESCRIPTION = (
    "``create`` writes a snapshot of both databases with a manifest (sizes and "
    "sha256), ``list`` shows the snapshots, ``restore`` puts one back. A "
    "restore asks for confirmation, refuses while the platform is running and "
    "snapshots the current state first, so it can itself be undone."
)


def register(parser: argparse.ArgumentParser, add_global_flags) -> None:
    """Declare the actions of ``sagaai backup``."""
    add_global_flags(parser)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    create = actions.add_parser(
        "create",
        help="snapshot both databases",
        epilog=exit_codes.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    create.add_argument("--label", default="",
                        help="a short note stored in the snapshot manifest")
    add_global_flags(create)

    listing = actions.add_parser(
        "list",
        help="list the stored snapshots",
        epilog=exit_codes.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_global_flags(listing)

    restore = actions.add_parser(
        "restore",
        help="restore one snapshot (asks for confirmation)",
        epilog=exit_codes.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    restore.add_argument("snapshot_id", metavar="ID",
                         help="snapshot id, as ``backup list`` shows it")
    restore.add_argument("--yes", action="store_true", dest="assume_yes",
                         help="do not ask for confirmation")
    restore.add_argument("--force", action="store_true",
                         help="restore even while the platform is running")
    add_global_flags(restore)
    parser.set_defaults(action="")


def _members_table(console, members) -> None:
    """Print the member rows of one manifest."""
    console.table(
        ["database", "state", "size", "integrity"],
        [[item.get("name"),
          "present" if item.get("present") else "absent",
          human_bytes(item.get("size")),
          item.get("integrity") or "-"] for item in members],
    )


def _create(args: argparse.Namespace, console) -> int:
    """Write a snapshot of both databases."""
    from core.gateway import backup

    report = _common.call(backup.create_backup,
                          label=str(getattr(args, "label", "") or ""))
    if not report.get("ok"):
        raise config_error("backup_failed",
                           "the snapshot failed: %s" % report.get("error"),
                           hint="check the data folder: sagaai doctor")
    data: Dict[str, Any] = {
        "id": report.get("id"),
        "path": os.path.join(backup.backups_root(), str(report.get("id") or "")),
        "created_at": report.get("created_at"),
        "label": report.get("label"),
        "members": list(report.get("members") or []),
    }
    if not console.json_mode:
        console.title("database snapshot")
        console.field("id", data["id"])
        console.field("folder", data["path"])
        if data["label"]:
            console.field("label", data["label"])
        console.blank()
        _members_table(console, data["members"])
    console.emit("backup create", data, exit_code=exit_codes.OK,
                 version=_common.platform_version())
    return exit_codes.OK


def _list(console) -> int:
    """List the stored snapshots, newest first."""
    from core.gateway import backup

    items = _common.call(backup.list_backups)
    if not console.json_mode:
        console.title("database snapshots")
        if not items:
            console.line("  no snapshots are stored")
            console.line("  create one: sagaai backup create")
        else:
            console.table(
                ["id", "created", "version", "label", "size"],
                [[item.get("id"), item.get("created_at"),
                  item.get("version"), item.get("label") or "",
                  human_bytes(sum(int(m.get("size") or 0)
                                  for m in item.get("members") or []))]
                 for item in items],
            )
    console.emit("backup list", {"snapshots": items, "count": len(items)},
                 exit_code=exit_codes.OK, version=_common.platform_version())
    return exit_codes.OK


def _restore(args: argparse.Namespace, console) -> int:
    """Restore one snapshot over the live databases."""
    from core.gateway import backup

    snapshot_id = str(getattr(args, "snapshot_id", "") or "").strip()
    if not backup.is_snapshot_id(snapshot_id):
        raise config_error("invalid_snapshot_id",
                           "not a snapshot id: " + snapshot_id,
                           hint="sagaai backup list")
    manifest = _common.call(backup.read_manifest, snapshot_id)
    if not manifest:
        raise config_error("unknown_snapshot",
                           "no snapshot with id " + snapshot_id,
                           hint="sagaai backup list")

    root = _common.install_root()
    _common.refuse_when_running(root, force=bool(getattr(args, "force", False)),
                                what="restoring a database snapshot")
    _common.confirm(
        "Restore snapshot %s over the live databases? The current state is "
        "snapshotted first." % snapshot_id,
        assume_yes=bool(getattr(args, "assume_yes", False)))

    report = _common.call(backup.restore_backup, snapshot_id)
    if not report.get("ok"):
        raise config_error("restore_failed",
                           "the snapshot was not restored: %s" % report.get("error"),
                           hint=("the databases were not replaced"
                                 if not report.get("restored") else
                                 "restored: " + ", ".join(report.get("restored") or [])))
    data: Dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "restored": list(report.get("restored") or []),
        "safety_snapshot": report.get("safety_snapshot") or "",
    }
    if not console.json_mode:
        console.title("restored")
        for name in data["restored"]:
            console.bullet(name)
        if data["safety_snapshot"]:
            console.blank()
            console.field("undo with", "sagaai backup restore " +
                          str(data["safety_snapshot"]))
        console.line("")
        console.line("  restart the platform to open the restored databases")
    console.emit("backup restore", data, exit_code=exit_codes.OK,
                 version=_common.platform_version())
    return exit_codes.OK


def execute(args: argparse.Namespace, console) -> int:
    """Dispatch to one of the three backup actions."""
    action = str(getattr(args, "action", "") or "")
    if action == "create":
        return _create(args, console)
    if action == "list":
        return _list(console)
    if action == "restore":
        return _restore(args, console)
    raise config_error("missing_action",
                       "choose an action: create, list or restore",
                       hint="sagaai backup --help")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
