# -*- coding: utf-8 -*-
"""
core.github_tools - orchestrator tools for GitHub connections.

Each function in this module is registered as a custom tool for an
orchestrator and follows the platform convention:

    invoke(**kwargs) -> dict

The first argument of every tool is ``connector_id`` - a connection id from
``core.connectors``. Tokens never travel in plain text: the connector layer
resolves and decrypts them before calling PyGithub. Errors are caught and
returned as {"ok": False, "error": ...} dicts so the dispatcher can feed them
back to the model.

Available tools:
    github_list_repos
    github_create_repo
    github_upload_file
    github_update_file
    github_read_file

No streamlit imports.
"""
from __future__ import annotations

from typing import Any, Dict

from core.github_connector import GithubConnectorError

__test__ = False  # pytest: these are tools, not unit-test functions


def _get_connector_id(kwargs: Dict[str, Any]) -> str:
    """Extract and validate the connector_id argument."""
    conn_id = str(kwargs.get("connector_id") or "").strip()
    if not conn_id:
        raise GithubConnectorError("Missing required argument: connector_id")
    return conn_id


def _wrap(fn, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Run a connector function; always return a plain dict."""
    try:
        # The inner closure already captured the validated arguments.
        return {"ok": True, "result": fn()}
    except GithubConnectorError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"GitHub tool failed: {e}"}


def github_list_repos(**kwargs: Any) -> Dict[str, Any]:
    """List repositories of the authenticated user.

    Arguments:
        connector_id (str, required): connection id.
        sort (str, optional): "updated" | "created" | "full_name".
    Returns:
        {"ok": True, "result": [{"full_name", "name", "private", ...}, ...]}
    """
    from core.github_connector import list_repos

    def run():
        conn_id = _get_connector_id(kwargs)
        sort = str(kwargs.get("sort") or "updated")
        return list_repos(conn_id, sort=sort)

    return _wrap(run, kwargs)


def github_create_repo(**kwargs: Any) -> Dict[str, Any]:
    """Create a new repository under the authenticated user.

    Arguments:
        connector_id (str, required): connection id.
        name (str, required): repository name (lowercase, no spaces).
        description (str, optional): repository description.
        private (bool, optional, default True): create a private repo.
    Returns:
        {"ok": True, "result": {"full_name", "name", "html_url", ...}}
    """
    from core.github_connector import create_repo

    def run():
        conn_id = _get_connector_id(kwargs)
        name = str(kwargs.get("name") or "").strip()
        if not name:
            raise GithubConnectorError("Missing required argument: name")
        description = str(kwargs.get("description") or "")
        private = bool(kwargs.get("private", True))
        return create_repo(conn_id, name, description=description, private=private)

    return _wrap(run, kwargs)


def github_upload_file(**kwargs: Any) -> Dict[str, Any]:
    """Create a new file in a repository.

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
        path (str, required): file path in the repository.
        content (str, required): file content.
        message (str, optional): commit message.
        branch (str, optional): target branch (default: default branch).
    Returns:
        {"ok": True, "result": {"path", "sha", "committed", "commit_sha"}}
    """
    from core.github_connector import upload_file

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        path = str(kwargs.get("path") or "").strip()
        content = str(kwargs.get("content") or "")
        if not repo:
            raise GithubConnectorError("Missing required argument: repo")
        if not path:
            raise GithubConnectorError("Missing required argument: path")
        return upload_file(
            conn_id,
            repo,
            path,
            content,
            message=str(kwargs.get("message") or ""),
            branch=str(kwargs.get("branch") or ""),
        )

    return _wrap(run, kwargs)


def github_update_file(**kwargs: Any) -> Dict[str, Any]:
    """Update an existing file in a repository.

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
        path (str, required): file path in the repository.
        content (str, required): new file content.
        message (str, optional): commit message.
        branch (str, optional): target branch.
        sha (str, optional): expected current file SHA; fetched when omitted.
    Returns:
        {"ok": True, "result": {"path", "sha", "committed", "commit_sha"}}
    """
    from core.github_connector import update_file

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        path = str(kwargs.get("path") or "").strip()
        content = str(kwargs.get("content") or "")
        if not repo:
            raise GithubConnectorError("Missing required argument: repo")
        if not path:
            raise GithubConnectorError("Missing required argument: path")
        return update_file(
            conn_id,
            repo,
            path,
            content,
            message=str(kwargs.get("message") or ""),
            branch=str(kwargs.get("branch") or ""),
            sha=str(kwargs.get("sha") or ""),
        )

    return _wrap(run, kwargs)


def github_read_file(**kwargs: Any) -> Dict[str, Any]:
    """Read a text file from a repository.

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
        path (str, required): file path in the repository.
        branch (str, optional): ref / branch to read from.
    Returns:
        {"ok": True, "result": {"path", "content", "sha", "url"}}
    """
    from core.github_connector import read_file

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        path = str(kwargs.get("path") or "").strip()
        if not repo:
            raise GithubConnectorError("Missing required argument: repo")
        if not path:
            raise GithubConnectorError("Missing required argument: path")
        return read_file(
            conn_id,
            repo,
            path,
            branch=str(kwargs.get("branch") or ""),
        )

    return _wrap(run, kwargs)


# Tool metadata for orchestrator catalogs: name -> description.
TOOLS = {}
TOOLS["github_list_repos"] = {
    "name": "github_list_repos",
    "desc": (
        "List GitHub repositories of the authenticated user. "
        "Arguments: connector_id (required), sort (optional)."
    ),
}
TOOLS["github_create_repo"] = {
    "name": "github_create_repo",
    "desc": (
        "Create a new GitHub repository. "
        "Arguments: connector_id (required), name (required), description, private."
    ),
}
TOOLS["github_upload_file"] = {
    "name": "github_upload_file",
    "desc": (
        "Upload (create) a new file in a GitHub repository. "
        "Arguments: connector_id, repo, path, content, message, branch."
    ),
}
TOOLS["github_update_file"] = {
    "name": "github_update_file",
    "desc": (
        "Update an existing file in a GitHub repository. "
        "Arguments: connector_id, repo, path, content, message, branch, sha."
    ),
}
TOOLS["github_read_file"] = {
    "name": "github_read_file",
    "desc": (
        "Read a text file from a GitHub repository. "
        "Arguments: connector_id, repo, path, branch."
    ),
}


def get_tools() -> list:
    """Return metadata for all GitHub tools (for orchestrator tool catalogs)."""
    return [TOOLS[name] for name in sorted(TOOLS)]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
