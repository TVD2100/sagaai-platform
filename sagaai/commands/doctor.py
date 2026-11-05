"""
sagaai doctor - what is wrong with this installation, without changing it.

The command is a formatter: every answer comes from
``core.gateway.diagnostics``, which is read-only by contract. Two output
shapes of the same report exist because they serve different readers - a
person pasting a terminal into a chat window, and a script deciding whether
to page somebody.

The exit code follows one rule: an ERROR makes the command fail, a WARNING
does not. A clean machine with no database and no token is a WARNING - that
is what an installation looks like before its first start - and reporting it
as a failure would teach people to ignore the code.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict

from sagaai import exit_codes
from sagaai.commands import _common
from sagaai.console import human_bytes, human_seconds

SUMMARY = "diagnose the installation without changing it"
DESCRIPTION = (
    "Checks the version, the interpreter, the data folders, the installed "
    "dependencies, both databases and their schema, the write permission of "
    "the data folder, the encryption key, the API tokens and the scheduler. "
    "Exit code 0 means no check failed; warnings (an installation that has "
    "never been started, for instance) do not make the command fail."
)


def register(parser: argparse.ArgumentParser, add_global_flags) -> None:
    """Declare the arguments of ``sagaai doctor``."""
    parser.add_argument("--verbose", action="store_true",
                        help="include the details of every check")
    add_global_flags(parser)


def _render_human(console, report: Dict[str, Any], verbose: bool) -> None:
    """Print the report as sections a person can read."""
    checks = report.get("checks") or {}
    console.title("SagaAI doctor")
    console.field("platform version", report.get("version") or "unknown")
    console.field("overall status", report.get("status"))
    console.blank()
    console.title("checks")
    for name, item in checks.items():
        console.status(name, str(item.get("status") or ""),
                       str(item.get("summary") or ""))
    if verbose:
        _render_details(console, checks)
    errors = list(report.get("errors") or [])
    if errors:
        console.blank()
        console.title("what to fix")
        for name in errors:
            summary = str((checks.get(name) or {}).get("summary") or "")
            console.bullet("%s: %s" % (name, summary))


def _render_details(console, checks: Dict[str, Any]) -> None:
    """Print the details of the checks that carry any."""
    paths = checks.get("paths") or {}
    if paths:
        console.blank()
        console.title("paths")
        console.field("base", paths.get("base_dir"))
        console.field("data", paths.get("data_dir"))
        for name, item in (paths.get("files") or {}).items():
            console.field(name, "%s (%s)" % (
                item.get("path"),
                human_bytes(item.get("size")) if item.get("exists") else "missing"))
    databases = (checks.get("databases") or {}).get("databases") or {}
    if databases:
        console.blank()
        console.title("databases")
        for name, item in databases.items():
            console.status(name, str(item.get("state") or ""),
                           "%s, %d table(s), revision=%s" % (
                               human_bytes(item.get("size")),
                               len(item.get("tables") or []),
                               item.get("revision") or "none"))
            missing = list(item.get("missing_tables") or [])
            if missing:
                console.bullet("missing tables: " + ", ".join(missing))
    schema = checks.get("schema") or {}
    if schema:
        console.blank()
        console.title("schema history")
        console.field("head", schema.get("head") or "unknown")
        for name, item in (schema.get("databases") or {}).items():
            console.status(name,
                           "ok" if item.get("current") == item.get("head") else
                           ("outdated" if item.get("behind") else "unversioned"),
                           "%s%s" % (item.get("current") or "no revision",
                                     (" (%d behind)" % item.get("behind"))
                                     if item.get("behind") else ""))
    tokens = checks.get("tokens") or {}
    if tokens:
        console.blank()
        console.title("api tokens")
        console.field("api enabled", tokens.get("enabled"))
        console.field("bind", "%s:%s" % (tokens.get("host"), tokens.get("port")))
        console.field("usable tokens", int(tokens.get("token_count") or 0)
                      - int(tokens.get("expired_count") or 0))
        for item in tokens.get("tokens") or []:
            console.bullet("%s  scope=%s  expires=%s%s" % (
                item.get("id"), ",".join(item.get("slugs") or []),
                item.get("expires_at") or "never",
                "  [expired]" if item.get("expired") else ""))
    scheduler = checks.get("scheduler") or {}
    if scheduler:
        console.blank()
        console.title("scheduler")
        console.field("rules", scheduler.get("rules"))
        console.field("heartbeat", scheduler.get("heartbeat_at") or "never")
        age = scheduler.get("heartbeat_age_s")
        if age is not None:
            console.field("heartbeat age", human_seconds(age))
    deps = checks.get("dependencies") or {}
    if deps:
        console.blank()
        console.title("dependencies")
        for item in deps.get("missing") or []:
            console.bullet("missing: %s%s" % (
                item.get("package"), " (required)" if item.get("required") else " (optional)"))
        for item in deps.get("outdated") or []:
            console.bullet("outdated: %s %s -> %s" % (
                item.get("package"), item.get("installed"), item.get("required")))
        if not (deps.get("missing") or deps.get("outdated")):
            console.bullet("every audited package is installed and current")
    key = checks.get("encryption_key") or {}
    if key:
        console.blank()
        console.title("encryption key")
        console.field("source", key.get("source"))
        console.field("path", key.get("path"))
        if key.get("mode"):
            console.field("mode", key.get("mode"))


def execute(args: argparse.Namespace, console) -> int:
    """Run every diagnostic check and report the result."""
    from core.gateway import diagnostics

    report = _common.call(diagnostics.doctor_report)
    version = str(report.get("version") or "")
    exit_code = exit_codes.OK if report.get("exit_ok") else exit_codes.FAILURE
    if not console.json_mode:
        _render_human(console, report, bool(getattr(args, "verbose", False)))
    # One emit per run: in JSON mode it writes the envelope, in human mode it
    # flushes the sections built above. Two calls would print the report twice.
    console.emit("doctor", report, exit_code=exit_code, version=version)
    return exit_code
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
