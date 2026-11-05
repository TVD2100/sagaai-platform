"""
sagaai serve - run the HTTP API of this installation.

The command does not start a server of its own: it performs the cold start
(so a fresh installation has its folders, its seed and its schema) and then
hands control to ``core.gateway_launcher``, which owns the bind rules, the
instance lock and the refusal messages. There is exactly one server
implementation and this command does not become a second one.

The bind address normally comes from the settings page - a port is a property
of the installation, and a flag that silently overrides it would let an
operator expose the API on a public interface by accident. The flags exist for
the one legitimate case: a supervisor (systemd, docker) that must own the
port. They are passed through the launcher's documented environment override
rather than by re-implementing its resolution, so the confirmation rules stay
in force.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict

from sagaai import exit_codes
from sagaai.commands import _common
from sagaai.errors import config_error

SUMMARY = "run the HTTP API of this installation"
DESCRIPTION = (
    "Starts the FastAPI server on the address configured on the Access page. "
    "The API must be enabled and must have a usable token; both are checked "
    "by the launcher, which also refuses a non-loopback bind without the "
    "explicit confirmation. Exit code 2 means the configuration forbids the "
    "start, 3 means another server already holds the instance lock."
)


def register(parser: argparse.ArgumentParser, add_global_flags) -> None:
    """Declare the arguments of ``sagaai serve``."""
    parser.add_argument("--host", default="",
                        help="bind address for this run (default: from settings)")
    parser.add_argument("--port", type=int, default=0,
                        help="bind port for this run (default: from settings)")
    parser.add_argument("--allow-remote", action="store_true",
                        dest="allow_remote",
                        help="confirm a non-loopback bind for this run")
    add_global_flags(parser)


def _apply_bind_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    """Pass the bind flags to the launcher through its documented env keys.

    Setting an environment variable in this process is the smallest possible
    change: the launcher keeps reading ONE source and its refusal rules - the
    remote-bind confirmation in particular - keep working exactly as written.
    """
    host = str(getattr(args, "host", "") or "").strip()
    port = int(getattr(args, "port", 0) or 0)
    applied: Dict[str, Any] = {}
    if host or port:
        from core import api_token_store as store
        current = store.load_settings()
        eff_host = host or str(current.get(store.HOST_KEY) or "127.0.0.1")
        eff_port = port or int(current.get(store.PORT_KEY) or 8765)
        os.environ["SAGAAI_API_BIND"] = "%s:%d" % (eff_host, eff_port)
        applied["host"] = eff_host
        applied["port"] = eff_port
    if bool(getattr(args, "allow_remote", False)):
        os.environ["SAGAAI_API_ALLOW_REMOTE"] = "1"
        applied["allow_remote"] = True
    return applied


def execute(args: argparse.Namespace, console) -> int:
    """Cold-start the installation and run the API in this process."""
    from core.gateway import bootstrap

    started = _common.call(bootstrap.cold_start)
    failed = list(started.get("failed") or [])
    if failed:
        raise config_error(
            "bootstrap_failed",
            "the installation could not be prepared: " + ", ".join(failed),
            hint="run: sagaai doctor --verbose",
        )

    applied = _apply_bind_overrides(args)
    port = int(applied.get("port") or 0)
    console.note("starting the HTTP API (Ctrl+C stops it)")
    try:
        from core.gateway_launcher import main as launcher_main
    except Exception as exc:  # pragma: no cover - the launcher ships with core
        raise config_error("launcher_unavailable",
                           "the launcher could not be imported: %s" % exc)

    display: Dict[str, Any] = {"bind": applied or "from settings",
                               "started": True}
    if not console.json_mode:
        console.title("SagaAI API")
        if applied:
            console.field("bind", "%s:%s" % (applied.get("host"), port))
        else:
            console.line("  bind: from the settings page")
        console.line("  the server writes its log to stderr")
    console.emit("serve", display, exit_code=exit_codes.OK,
                 version=_common.platform_version())

    try:
        code = int(launcher_main([]) or 0)
    except SystemExit as exc:
        code = int(exc.code or 0)
    if code == 2:
        raise config_error("api_refused",
                           "the launcher refused to start the API",
                           hint="see the message above: enable the API, create "
                                "a token, or confirm a non-loopback bind")
    if code == 3:
        from sagaai.errors import conflict_error
        raise conflict_error("already_running",
                             "another HTTP server already holds the lock",
                             hint="stop it first: it is a separate process")
    return exit_codes.OK if code == 0 else exit_codes.FAILURE
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
