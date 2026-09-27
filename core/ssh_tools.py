# -*- coding: utf-8 -*-
"""
core.ssh_tools - orchestrator tools for the SSH connector.

Tool layer for ``core.ssh_connector``. Follows the platform convention
for orchestrator tools:

    invoke(**kwargs) -> dict

The first argument of every tool is ``connector_id`` - a connection id from
``core.connectors`` (service ``ssh``). Credentials never travel in plain
text: the connector layer resolves and decrypts them before opening the SSH
session. Failures are caught and returned as ``{"ok": False, "error": ...}``
dicts so the dispatcher can feed them back to the model.

Tools return the connector payload under the ``result`` key. Everything is
read-only except ``ssh_write_file``, which overwrites the remote file.

No streamlit imports.
"""
from __future__ import annotations

from typing import Any, Dict

from core.ssh_connector import SSHConnectorError

__test__ = False  # pytest: these are tools, not unit-test functions


def _get_connector_id(kwargs: Dict[str, Any]) -> str:
    """Extract and validate the connector_id argument."""
    conn_id = str(kwargs.get("connector_id") or "").strip()
    if not conn_id:
        raise SSHConnectorError("Missing required argument: connector_id")
    return conn_id


def _wrap(fn) -> Dict[str, Any]:
    """Run a connector function; always return a plain dict."""
    try:
        return {"ok": True, "result": fn()}
    except SSHConnectorError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"SSH tool failed: {e}"}


def ssh_test_connection(**kwargs: Any) -> Dict[str, Any]:
    """Validate an SSH connection and refresh its account info.

    Arguments:
        connector_id (str, required): connection id.
    Returns:
        {"ok": True, "result": {"ok", "host", "port", "username"}}
    """
    from core.ssh_connector import test_connection

    def run():
        return test_connection(_get_connector_id(kwargs))

    return _wrap(run)


def ssh_exec(**kwargs: Any) -> Dict[str, Any]:
    """Run a shell command on a remote SSH server.

    Arguments:
        connector_id (str, required): connection id.
        command (str, required): shell command to execute.
        timeout (number | str, optional): seconds to wait (default 60, max 300).
    Returns:
        {"ok": True, "result": {"command", "exit_status", "stdout",
        "stderr", "stdout_truncated", "stderr_truncated", "timeout_seconds"}}
    """
    from core.ssh_connector import exec_command

    def run():
        conn_id = _get_connector_id(kwargs)
        command = str(kwargs.get("command") or "")
        if not command.strip():
            raise SSHConnectorError("Missing required argument: command")
        return exec_command(conn_id, command, timeout=kwargs.get("timeout"))

    return _wrap(run)


def ssh_list_dir(**kwargs: Any) -> Dict[str, Any]:
    """List a remote directory over SFTP.

    Arguments:
        connector_id (str, required): connection id.
        path (str, optional): remote directory (default ".").
    Returns:
        {"ok": True, "result": [{"name", "path", "type", "size", "mtime"}, ...]}
    """
    from core.ssh_connector import list_dir

    def run():
        conn_id = _get_connector_id(kwargs)
        path = str(kwargs.get("path") or ".").strip() or "."
        return list_dir(conn_id, path)

    return _wrap(run)


def ssh_read_file(**kwargs: Any) -> Dict[str, Any]:
    """Read a UTF-8 text file from a remote SSH server (capped at 256 KB).

    Arguments:
        connector_id (str, required): connection id.
        path (str, required): remote file path.
    Returns:
        {"ok": True, "result": {"path", "content", "size", "truncated"}}
    """
    from core.ssh_connector import read_file

    def run():
        conn_id = _get_connector_id(kwargs)
        path = str(kwargs.get("path") or "").strip()
        if not path:
            raise SSHConnectorError("Missing required argument: path")
        return read_file(conn_id, path)

    return _wrap(run)


def ssh_write_file(**kwargs: Any) -> Dict[str, Any]:
    """Write a UTF-8 text file to a remote SSH server (capped at 1 MB).

    Overwrites the remote file content. Arguments:
        connector_id (str, required): connection id.
        path (str, required): remote file path.
        content (str, required): full file content.
        create_dirs (bool, optional): create the remote parent chain
            (mkdir -p style) before writing.
    Returns:
        {"ok": True, "result": {"path", "size", "written"}}
    """
    from core.ssh_connector import write_file

    def run():
        conn_id = _get_connector_id(kwargs)
        path = str(kwargs.get("path") or "").strip()
        if not path:
            raise SSHConnectorError("Missing required argument: path")
        if kwargs.get("content") is None:
            raise SSHConnectorError("Missing required argument: content")
        return write_file(
            conn_id, path, str(kwargs.get("content")),
            create_dirs=bool(kwargs.get("create_dirs", False)),
        )

    return _wrap(run)


# ─── Tool catalog metadata ──────────────────────────────────────────────────

TOOLS: Dict[str, Dict[str, str]] = {}

TOOLS["ssh_test_connection"] = {
    "name": "ssh_test_connection",
    "desc": (
        "Validate an SSH connection and refresh its account info. "
        "Arguments: connector_id (required)."
    ),
}
TOOLS["ssh_exec"] = {
    "name": "ssh_exec",
    "desc": (
        "Run a shell command on a remote SSH server; returns exit status, "
        "stdout and stderr (each truncated to 100 KB). "
        "Arguments: connector_id (required), command (required), "
        "timeout (optional seconds, max 300)."
    ),
}
TOOLS["ssh_list_dir"] = {
    "name": "ssh_list_dir",
    "desc": (
        "List a remote directory over SFTP (name, path, type, size, mtime). "
        "Arguments: connector_id (required), path (optional, default '.')."
    ),
}
TOOLS["ssh_read_file"] = {
    "name": "ssh_read_file",
    "desc": (
        "Read a UTF-8 text file from a remote SSH server (capped at 256 KB). "
        "Arguments: connector_id (required), path (required)."
    ),
}
TOOLS["ssh_write_file"] = {
    "name": "ssh_write_file",
    "desc": (
        "Write a UTF-8 text file to a remote SSH server, overwriting it "
        "(capped at 1 MB). Arguments: connector_id (required), path "
        "(required), content (required), create_dirs (optional bool)."
    ),
}


def get_tools() -> list:
    """Return metadata for all SSH tools (for orchestrator catalogs)."""
    return [TOOLS[name] for name in sorted(TOOLS)]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
