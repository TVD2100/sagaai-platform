"""
sagaai ui - open the Streamlit interface of this installation.

The interface is started as a CHILD PROCESS and this module never imports
streamlit. Three reasons, and each is enough on its own:

- importing streamlit has side effects (it registers pages, reads a config and
ows a session object) that make no sense in a console command;
- a crash of the interface must not take down a supervisor that started the
console;
- the layering rule "the console does not need the UI" then holds without an
exception, and a test can prove it by blocking the import.

The child inherits stdin, stdout and stderr, so the interface behaves exactly
as if the user had run ``streamlit run app.py`` themselves - Ctrl+C, the URL
line and the browser window included.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import List

from sagaai import exit_codes
from sagaai.commands import _common
from sagaai.errors import config_error

SUMMARY = "open the Streamlit interface"
DESCRIPTION = (
    "Starts ``streamlit run app.py`` as a child process in the installation "
    "folder. The interface is the only surface that imports streamlit; the "
    "console never does, so this command works by launching it, not by "
    "loading it. Arguments after ``--`` are passed to streamlit unchanged."
)


def register(parser: argparse.ArgumentParser, add_global_flags) -> None:
    """Declare the arguments of ``sagaai ui``."""
    parser.add_argument("--port", type=int, default=0,
                        help="port of the interface (default: 8501)")
    parser.add_argument("--headless", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("extra", nargs="*",
                        help="arguments passed to streamlit after --")
    add_global_flags(parser)


def _streamlit_available(executable: str) -> bool:
    """True when ``python -m streamlit`` resolves in this interpreter."""
    try:
        result = subprocess.run(
            [executable, "-c", "import streamlit; print(streamlit.__version__)"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _command(executable: str, app_path: str, args: argparse.Namespace) -> List[str]:
    """Build the child command line from the parsed arguments."""
    command = [executable, "-m", "streamlit", "run", app_path]
    port = int(getattr(args, "port", 0) or 0)
    if port:
        command.extend(["--server.port", str(port)])
    if bool(getattr(args, "headless", False)):
        command.extend(["--server.headless", "true"])
    command.extend(str(item) for item in (getattr(args, "extra", None) or []))
    return command


def execute(args: argparse.Namespace, console) -> int:
    """Start the interface as a child process and return its exit code."""
    from core.gateway import bootstrap

    started = _common.call(bootstrap.cold_start)
    failed = list(started.get("failed") or [])
    if failed:
        raise config_error(
            "bootstrap_failed",
            "the installation could not be prepared: " + ", ".join(failed),
            hint="run: sagaai doctor --verbose",
        )

    root = _common.install_root()
    app_path = os.path.join(root, "app.py")
    if not os.path.isfile(app_path):
        raise config_error("app_missing", "app.py was not found in " + root,
                           hint="run the command from the installation folder")
    executable = sys.executable or "python3"
    if not _streamlit_available(executable):
        raise config_error(
            "streamlit_missing",
            "streamlit is not installed in this interpreter (%s)" % executable,
            hint="pip install -r requirements.lock",
        )

    command = _command(executable, app_path, args)
    port = int(getattr(args, "port", 0) or 0) or 8501
    payload = {"command": command, "port": port,
               "url": "http://localhost:%d" % port, "started": True}
    if not console.json_mode:
        console.title("SagaAI interface")
        console.field("folder", root)
        console.field("url", payload["url"])
        console.line("  Ctrl+C stops the interface")
    console.emit("ui", payload, exit_code=exit_codes.OK,
                 version=_common.platform_version())

    try:
        result = subprocess.run(command, cwd=root)
    except KeyboardInterrupt:
        return exit_codes.OK
    except OSError as exc:
        raise config_error("launch_failed",
                           "the interface could not be started: %s" % exc)
    return int(result.returncode or 0)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
