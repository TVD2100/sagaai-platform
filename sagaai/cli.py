"""
sagaai.cli - the argument parser and the dispatcher.

One function builds the parser, one runs a command, and everything that
concerns a SPECIFIC command lives in its module under ``sagaai/commands``.
This file knows only the shared contract:

- the global keys ``--json`` and ``--version``, accepted after every command
  and subcommand;
- the registry of commands as module names imported on demand, so ``--help``
  never imports a transport it does not need;
- the single place that turns a command's return value or its exception into
  an exit code and a printed report.

The command name recorded in the JSON envelope is the full path a person
typed (``token create``), not the module name: a script that parses the
envelope should recognise the command the way it invoked it.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any, Dict, List, Optional

from sagaai import __version__ as cli_version
from sagaai import exit_codes
from sagaai.console import Console
from sagaai.errors import CliError

#: Command name -> module under ``sagaai.commands``. A string, not an object:
#: ``--help`` must not import the command modules (``serve`` pulls the HTTP
#: transport, ``ui`` shells out to streamlit), and the isolation test relies
#: on the registry being importable with every third-party package blocked.
COMMANDS: Dict[str, str] = {
    "doctor": "doctor",
    "run": "run",
    "serve": "serve",
    "ui": "ui",
    "token": "token",
    "update": "update",
    "migrate": "migrate",
    "backup": "backup",
    "agents": "agents",
}


def platform_version() -> str:
    """Return the platform version, or an empty string when it is unreadable."""
    try:
        from core.version import __version__
        return str(__version__)
    except Exception:  # pragma: no cover - version is informational
        return ""


# ─── Parser ─────────────────────────────────────────────────────────────────


def add_global_flags(parser: argparse.ArgumentParser) -> None:
    """Add the shared options to a command parser.

    Called for the command itself and for each of its actions: ``--json`` must
    be accepted after ``sagaai token create`` as well as after ``sagaai token``.
    """
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help="machine-readable output: one JSON object on stdout")


def build_parser() -> argparse.ArgumentParser:
    """Build the whole parser tree, importing every command module.

    Only the command modules are imported, never their heavyweight
dependencies: a module imports ``core.gateway`` at the top and its transport
(uvicorn, streamlit) inside the function that needs it.
    """
    parser = argparse.ArgumentParser(
        prog="sagaai",
        description="SagaAI platform console - a second, equal entry point "
                    "next to the web interface and the HTTP API.",
        epilog=exit_codes.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help="machine-readable output: one JSON object on stdout")
    parser.add_argument("--version", action="store_true", dest="show_version",
                        help="print the platform version and exit")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for name in COMMANDS:
        module = importlib.import_module("sagaai.commands." + COMMANDS[name])
        sub = subparsers.add_parser(
            name,
            help=getattr(module, "SUMMARY", name),
            description=getattr(module, "DESCRIPTION", None),
            epilog=exit_codes.HELP_EPILOG,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        # The command declares its own arguments AND the shared ``--json``:
        # adding the flag here as well would be a duplicate option string.
        module.register(sub, add_global_flags)
    return parser


# ─── Dispatch ───────────────────────────────────────────────────────────────


def _full_command(args: argparse.Namespace) -> str:
    """Return the command path as the user typed it (``token create``)."""
    name = str(getattr(args, "command", "") or "")
    action = str(getattr(args, "action", "") or "")
    return (name + " " + action).strip() if action else name


def run_command(args: argparse.Namespace, console: Console) -> int:
    """Execute one parsed command, translating its failure into a code.

    The command returns the code; a raised :class:`CliError` becomes the
    printed error block and its code. A gateway refusal is translated here -
    the command itself does not know about HTTP statuses.
    """
    name = str(getattr(args, "command", "") or "")
    if not name:
        return exit_codes.CONFIG
    module = importlib.import_module("sagaai.commands." + COMMANDS[name])
    full = _full_command(args)
    try:
        return int(module.execute(args, console) or 0)
    except CliError as exc:
        console.emit_error(full, exc, exit_code=exc.exit_code,
                           version=platform_version())
        return exc.exit_code
    except KeyboardInterrupt:
        error = CliError("interrupted", "interrupted by the user",
                         exit_code=exit_codes.FAILURE)
        console.emit_error(full, error, exit_code=error.exit_code,
                           version=platform_version())
        return error.exit_code
    except Exception as exc:  # noqa: BLE001 - the boundary must not leak a traceback
        if _is_gateway_error(exc):
            from sagaai.errors import from_gateway
            translated = from_gateway(exc)
            console.emit_error(full, translated, exit_code=translated.exit_code,
                               version=platform_version())
            return translated.exit_code
        error = CliError("internal_error", "%s: %s" % (type(exc).__name__, exc),
                         exit_code=exit_codes.FAILURE,
                         hint="run: sagaai doctor")
        console.emit_error(full, error, exit_code=error.exit_code,
                           version=platform_version())
        return error.exit_code


def _is_gateway_error(exc: BaseException) -> bool:
    """Return True when *exc* looks like a ``GatewayError``.

    Duck-typed: ``sagaai.errors`` already avoids importing the class, and a
    subclass of it is still a refusal with the same public attributes.
    """
    return all(hasattr(exc, attribute)
               for attribute in ("code", "message", "status"))


def main(argv: Optional[List[str]] = None) -> int:
    """Run the console; return the process exit code.

    ``--version`` is handled before the parser tree is built, so it works even
    when a command module cannot import (a missing optional dependency of one
    command must not hide the version of the installation).
    """
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in ("--version", "-V"):
        print(platform_version() or cli_version)
        return exit_codes.OK

    parser = build_parser()
    try:
        args = parser.parse_args(raw)
    except SystemExit as exc:  # argparse already printed its own message
        return int(exc.code or 0)

    console = Console(json_mode=bool(getattr(args, "json_mode", False)))
    if getattr(args, "show_version", False):
        data: Dict[str, Any] = {"version": platform_version(),
                                "cli_version": cli_version}
        console.emit("version", data, exit_code=exit_codes.OK,
                     version=platform_version())
        return exit_codes.OK
    if not getattr(args, "command", ""):
        parser.print_help()
        return exit_codes.CONFIG
    return run_command(args, console)


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
