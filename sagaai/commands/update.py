"""
sagaai update - the platform update pipeline from the console.

The pipeline itself already exists and is finished: ``core.updater`` fetches
the manifest and stages the files with a sha256 check, ``core.updater_apply``
replaces them with a per-file backup and rolls the whole run back on any
failure. This command adds what a console session needs and nothing else:

- a confirmation for the two DESTRUCTIVE actions (apply, rollback), because a
  console session is where a mistaken keystroke replaces code;
- a refusal while the platform is running (exit 3), because the running
  process has already imported the files a replacement would swap;
- a human report of what was applied and where the backup went, which is the
  one thing an operator needs after a failed update.

``check`` and ``stage`` change nothing that runs (staging writes only into
``.dev_agent/updates/pending``), so they need no confirmation.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List

from sagaai import exit_codes
from sagaai.commands import _common
from sagaai.errors import CliError, config_error

SUMMARY = "check, stage, apply and roll back platform updates"
DESCRIPTION = (
    "The four steps of the update pipeline. ``check`` compares the installed "
    "files with the published manifest, ``stage`` downloads the changed ones "
    "with a sha256 check, ``apply`` replaces them with a backup of every file "
    "it touches, ``rollback`` restores the previous version. The channel is "
    "the public manifest URL; a local mirror can be given instead."
)


def register(parser: argparse.ArgumentParser, add_global_flags) -> None:
    """Declare the actions of ``sagaai update``."""
    add_global_flags(parser)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    check = actions.add_parser(
        "check",
        help="compare the installed files with the published manifest",
        epilog=exit_codes.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    check.add_argument("--channel", default="",
                       help="manifest base URL or a local folder mirror")
    add_global_flags(check)

    stage = actions.add_parser(
        "stage",
        help="download the changed files into the pending store",
        epilog=exit_codes.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    stage.add_argument("--channel", default="",
                       help="manifest base URL or a local folder mirror")
    stage.add_argument("--only", nargs="*", metavar="PATH",
                       help="stage only these paths (default: everything "
                            "reported by check)")
    add_global_flags(stage)

    apply_parser = actions.add_parser(
        "apply",
        help="apply the staged files (asks for confirmation)",
        epilog=exit_codes.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    apply_parser.add_argument("--yes", action="store_true", dest="assume_yes",
                              help="do not ask for confirmation")
    apply_parser.add_argument("--force", action="store_true",
                              help="apply even while the platform is running")
    add_global_flags(apply_parser)

    rollback = actions.add_parser(
        "rollback",
        help="restore the files of the last applied run",
        epilog=exit_codes.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    rollback.add_argument("rel", nargs="?", default=None, metavar="PATH",
                          help="restore one file instead of the whole run")
    rollback.add_argument("--yes", action="store_true", dest="assume_yes",
                          help="do not ask for confirmation")
    rollback.add_argument("--force", action="store_true",
                          help="roll back even while the platform is running")
    add_global_flags(rollback)
    parser.set_defaults(action="")


def _channel(args: argparse.Namespace) -> str:
    """Return the channel to use, or the built-in default."""
    from core.updater import DEFAULT_CHANNEL
    return str(getattr(args, "channel", "") or "") or DEFAULT_CHANNEL


def _check(args: argparse.Namespace, console) -> int:
    """Compare the installation with the manifest."""
    from core.updater import check_updates

    root = _common.install_root()
    report = _common.call(check_updates, root, channel=_channel(args))
    if not report.get("ok"):
        raise CliError("check_failed", str(report.get("error") or "check failed"),
                       exit_code=exit_codes.FAILURE,
                       hint="a network error or an unreachable channel")
    available: List[Dict[str, Any]] = list(report.get("available") or [])
    version = report.get("app_version") or {}
    data: Dict[str, Any] = {"app_version": version, "available": available,
                            "up_to_date": bool(report.get("up_to_date")),
                            "source": report.get("source") or ""}
    if not console.json_mode:
        console.title("SagaAI updates")
        console.field("installed version", version.get("local"))
        console.field("published version", version.get("remote"))
        console.field("channel", report.get("source"))
        console.blank()
        if available:
            console.title("%d file(s) to update" % len(available))
            console.table(
                ["action", "path", "version"],
                [[item.get("action"), item.get("path"),
                  item.get("version")] for item in available],
            )
            console.line("")
            console.line("  stage them: sagaai update stage")
        else:
            console.line("  everything is up to date")
    console.emit("update check", data, exit_code=exit_codes.OK,
                 version=_common.platform_version())
    return exit_codes.OK


def _stage(args: argparse.Namespace, console) -> int:
    """Download the changed files into the pending store."""
    from core.updater import stage_updates

    root = _common.install_root()
    selection = list(getattr(args, "only", None) or []) or None
    report = _common.call(stage_updates, root, selection=selection,
                          channel=_channel(args))
    staged = list(report.get("staged") or [])
    if not report.get("ok"):
        raise CliError("stage_failed", str(report.get("error") or "staging failed"),
                       exit_code=exit_codes.FAILURE,
                       hint="nothing outside the pending store was changed",
                       )
    data = {"staged": staged, "count": len(staged),
            "pending_path": report.get("pending_path") or ""}
    if not console.json_mode:
        console.title("staged updates")
        if staged:
            for rel in staged:
                console.bullet(rel)
            console.blank()
            console.line("  apply them: sagaai update apply")
        else:
            console.line("  nothing to stage: the installation is up to date")
    console.emit("update stage", data, exit_code=exit_codes.OK,
                 version=_common.platform_version())
    return exit_codes.OK


def _apply(args: argparse.Namespace, console) -> int:
    """Apply the staged files, with confirmation and a running check."""
    from core.updater import apply_updates

    root = _common.install_root()
    _common.refuse_when_running(root, force=bool(getattr(args, "force", False)),
                                what="applying an update")
    _common.confirm("Apply the staged update files? A backup of every replaced "
                    "file is taken first.",
                    assume_yes=bool(getattr(args, "assume_yes", False)))
    report = _common.call(apply_updates, root,
                          force=bool(getattr(args, "force", False)))
    applied = list(report.get("applied") or [])
    if not report.get("ok"):
        raise CliError("apply_failed", str(report.get("error") or "apply failed"),
                       exit_code=exit_codes.FAILURE,
                       hint="the run was rolled back; health: " +
                            str(report.get("health_path") or "-"))
    data = {"applied": applied, "count": len(applied),
            "health_path": report.get("health_path") or ""}
    if not console.json_mode:
        console.title("applied updates")
        if applied:
            for rel in applied:
                console.bullet(rel)
        else:
            console.line("  nothing was staged")
        console.line("")
        console.line("  restart the platform to load the new code")
    console.emit("update apply", data, exit_code=exit_codes.OK,
                 version=_common.platform_version())
    return exit_codes.OK


def _rollback(args: argparse.Namespace, console) -> int:
    """Restore one file or the whole last applied run."""
    from core.updater import rollback_updates

    root = _common.install_root()
    rel = getattr(args, "rel", None)
    _common.refuse_when_running(root, force=bool(getattr(args, "force", False)),
                                what="rolling an update back")
    target = ("the file " + rel) if rel else "the last applied run"
    _common.confirm("Restore %s from its backup?" % target,
                    assume_yes=bool(getattr(args, "assume_yes", False)))
    report = _common.call(rollback_updates, root, rel=rel,
                          force=bool(getattr(args, "force", False)))
    restored = list(report.get("restored") or [])
    if not report.get("ok"):
        raise CliError("rollback_failed",
                       str(report.get("error") or "rollback failed"),
                       exit_code=exit_codes.FAILURE)
    data = {"restored": restored, "count": len(restored)}
    if not console.json_mode:
        console.title("restored")
        if restored:
            for rel_path in restored:
                console.bullet(rel_path)
        else:
            console.line("  nothing to restore: no backup run was found")
    console.emit("update rollback", data, exit_code=exit_codes.OK,
                 version=_common.platform_version())
    return exit_codes.OK


def execute(args: argparse.Namespace, console) -> int:
    """Dispatch to one of the four update actions."""
    action = str(getattr(args, "action", "") or "")
    if action == "check":
        return _check(args, console)
    if action == "stage":
        return _stage(args, console)
    if action == "apply":
        return _apply(args, console)
    if action == "rollback":
        return _rollback(args, console)
    raise config_error("missing_action",
                       "choose an action: check, stage, apply or rollback",
                       hint="sagaai update --help")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
