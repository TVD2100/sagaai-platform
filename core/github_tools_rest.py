# -*- coding: utf-8 -*-
"""
core.github_tools_rest - orchestrator tools for the REST GitHub connector.

Tool layer for ``core.github_connector_rest``. Follows the platform
convention for orchestrator tools:

    invoke(**kwargs) -> dict

The first argument of every tool is ``connector_id`` - a connection id from
``core.connectors`` (service ``github_rest``). Tokens never travel in plain
text: the connector layer resolves and decrypts them before performing HTTP
calls. Errors are caught and returned as {"ok": False, "error": ...} dicts
so the dispatcher can feed them back to the model.

Batch tools accept ``files`` either as a list of {"path", "content"}
dictionaries or as a JSON string with the same structure.

No streamlit imports.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from core.github_connector_rest import GithubRestError

__test__ = False  # pytest: these are tools, not unit-test functions


def _get_connector_id(kwargs: Dict[str, Any]) -> str:
    """Extract and validate the connector_id argument."""
    conn_id = str(kwargs.get("connector_id") or "").strip()
    if not conn_id:
        raise GithubRestError("Missing required argument: connector_id")
    return conn_id


def _wrap(fn, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Run a connector function; always return a plain dict."""
    try:
        return {"ok": True, "result": fn()}
    except GithubRestError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"GitHub REST tool failed: {e}"}


def _files_arg(kwargs: Dict[str, Any], tool_name: str) -> List[Dict[str, Any]]:
    """Normalize the ``files`` argument (list of dicts or JSON string)."""
    files = kwargs.get("files")
    if isinstance(files, str):
        text = files.strip()
        if not text:
            raise GithubRestError(f"Missing required argument: files ({tool_name})")
        try:
            files = json.loads(text)
        except ValueError:
            raise GithubRestError("Argument 'files' is not valid JSON")
    if not isinstance(files, list) or not files:
        raise GithubRestError(f"Missing required argument: files ({tool_name})")
    return files


def ghr_list_repos(**kwargs: Any) -> Dict[str, Any]:
    """List repositories of the authenticated user.

    Arguments:
        connector_id (str, required): connection id.
        sort (str, optional): "updated" | "created" | "full_name".
    Returns:
        {"ok": True, "result": [{"full_name", "name", "private", ...}, ...]}
    """
    from core.github_connector_rest import list_repos

    def run():
        conn_id = _get_connector_id(kwargs)
        return list_repos(conn_id, sort=str(kwargs.get("sort") or "updated"))

    return _wrap(run, kwargs)


def ghr_create_repo(**kwargs: Any) -> Dict[str, Any]:
    """Create a new repository under the authenticated user.

    Arguments:
        connector_id (str, required): connection id.
        name (str, required): repository name (lowercase, no spaces).
        description (str, optional): repository description.
        private (bool, optional, default True): create a private repo.
    Returns:
        {"ok": True, "result": {"full_name", "name", "html_url", ...}}
    """
    from core.github_connector_rest import create_repo

    def run():
        conn_id = _get_connector_id(kwargs)
        name = str(kwargs.get("name") or "").strip()
        if not name:
            raise GithubRestError("Missing required argument: name")
        return create_repo(
            conn_id,
            name,
            description=str(kwargs.get("description") or ""),
            private=bool(kwargs.get("private", True)),
            auto_init=bool(kwargs.get("auto_init", True)),
        )

    return _wrap(run, kwargs)


def ghr_read_file(**kwargs: Any) -> Dict[str, Any]:
    """Read a text file from a repository.

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
        path (str, required): file path in the repository.
        branch (str, optional): ref / branch to read from.
    Returns:
        {"ok": True, "result": {"path", "content", "sha", "url"}}
    """
    from core.github_connector_rest import read_file

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        path = str(kwargs.get("path") or "").strip()
        if not repo:
            raise GithubRestError("Missing required argument: repo")
        if not path:
            raise GithubRestError("Missing required argument: path")
        return read_file(conn_id, repo, path,
                         branch=str(kwargs.get("branch") or ""))

    return _wrap(run, kwargs)


def ghr_upload_file(**kwargs: Any) -> Dict[str, Any]:
    """Create a NEW file in a repository (one commit via the Contents API).

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
        path (str, required): file path in the repository.
        content (str, required): file content.
        message (str, optional): commit message.
        branch (str, optional): target branch.
    Returns:
        {"ok": True, "result": {"path", "sha", "committed", "commit_sha"}}
    """
    from core.github_connector_rest import upload_file

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        path = str(kwargs.get("path") or "").strip()
        content = str(kwargs.get("content") or "")
        if not repo:
            raise GithubRestError("Missing required argument: repo")
        if not path:
            raise GithubRestError("Missing required argument: path")
        return upload_file(
            conn_id, repo, path, content,
            message=str(kwargs.get("message") or ""),
            branch=str(kwargs.get("branch") or ""),
        )

    return _wrap(run, kwargs)


def ghr_update_file(**kwargs: Any) -> Dict[str, Any]:
    """Update an existing file (one commit via the Contents API).

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
    from core.github_connector_rest import update_file

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        path = str(kwargs.get("path") or "").strip()
        content = str(kwargs.get("content") or "")
        if not repo:
            raise GithubRestError("Missing required argument: repo")
        if not path:
            raise GithubRestError("Missing required argument: path")
        return update_file(
            conn_id, repo, path, content,
            message=str(kwargs.get("message") or ""),
            branch=str(kwargs.get("branch") or ""),
            sha=str(kwargs.get("sha") or ""),
        )

    return _wrap(run, kwargs)


def ghr_delete_file(**kwargs: Any) -> Dict[str, Any]:
    """Delete a file from a repository (one commit via the Contents API).

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
        path (str, required): file path in the repository.
        message (str, optional): commit message.
        branch (str, optional): target branch.
        sha (str, optional): file SHA; fetched when omitted.
    Returns:
        {"ok": True, "result": {"path", "committed"}}
    """
    from core.github_connector_rest import delete_file

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        path = str(kwargs.get("path") or "").strip()
        if not repo:
            raise GithubRestError("Missing required argument: repo")
        if not path:
            raise GithubRestError("Missing required argument: path")
        return delete_file(
            conn_id, repo, path,
            message=str(kwargs.get("message") or ""),
            branch=str(kwargs.get("branch") or ""),
            sha=str(kwargs.get("sha") or ""),
        )

    return _wrap(run, kwargs)


def ghr_list_files(**kwargs: Any) -> Dict[str, Any]:
    """List the top-level entries of a repository directory.

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
        path (str, optional): directory path ("" = repository root).
        branch (str, optional): ref / branch.
    Returns:
        {"ok": True, "result": [{"name", "path", "type"}, ...]}
    """
    from core.github_connector_rest import list_files

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        if not repo:
            raise GithubRestError("Missing required argument: repo")
        return list_files(
            conn_id, repo,
            path=str(kwargs.get("path") or ""),
            branch=str(kwargs.get("branch") or ""),
        )

    return _wrap(run, kwargs)


def ghr_test_connection(**kwargs: Any) -> Dict[str, Any]:
    """Validate a GitHub REST connection and refresh its account info.

    Arguments:
        connector_id (str, required): connection id.
    Returns:
        {"ok": True, "result": {"ok": True, "login", "name", "id",
        "html_url"}}
    """
    from core.github_connector_rest import test_connection

    def run():
        conn_id = _get_connector_id(kwargs)
        return test_connection(conn_id)

    return _wrap(run, kwargs)


def ghr_get_repo_info(**kwargs: Any) -> Dict[str, Any]:
    """Return metadata for one repository.

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
    Returns:
        {"ok": True, "result": {"full_name", "name", "owner", "private",
        "default_branch", ...}}
    """
    from core.github_connector_rest import get_repo_info

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        if not repo:
            raise GithubRestError("Missing required argument: repo")
        return get_repo_info(conn_id, repo)

    return _wrap(run, kwargs)


def ghr_read_file_meta(**kwargs: Any) -> Dict[str, Any]:
    """Return file metadata (sha, size) without the file content.

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
        path (str, required): file path in the repository.
        branch (str, optional): ref / branch to read from.
    Returns:
        {"ok": True, "result": {"path", "sha", "size", "url"}}
    """
    from core.github_connector_rest import read_file_meta

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        path = str(kwargs.get("path") or "").strip()
        if not repo:
            raise GithubRestError("Missing required argument: repo")
        if not path:
            raise GithubRestError("Missing required argument: path")
        return read_file_meta(conn_id, repo, path,
                              branch=str(kwargs.get("branch") or ""))

    return _wrap(run, kwargs)


def ghr_get_ref(**kwargs: Any) -> Dict[str, Any]:
    """Return a branch head as {"ref", "sha", "object_type"}.

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
        branch (str, optional): branch name (default branch when empty).
        resolve (bool, optional, default True): resolve the ref.
    Returns:
        {"ok": True, "result": {"ref", "sha", "object_type"}}
    """
    from core.github_connector_rest import get_ref

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        if not repo:
            raise GithubRestError("Missing required argument: repo")
        return get_ref(
            conn_id, repo,
            branch=str(kwargs.get("branch") or ""),
            resolve=bool(kwargs.get("resolve", True)),
        )

    return _wrap(run, kwargs)


def ghr_get_commit(**kwargs: Any) -> Dict[str, Any]:
    """Return a Git commit object.

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
        commit_sha (str, required): commit SHA.
    Returns:
        {"ok": True, "result": {"sha", "message", "tree_sha", "parents"}}
    """
    from core.github_connector_rest import get_commit

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        commit_sha = str(kwargs.get("commit_sha") or "").strip()
        if not repo:
            raise GithubRestError("Missing required argument: repo")
        if not commit_sha:
            raise GithubRestError("Missing required argument: commit_sha")
        return get_commit(conn_id, repo, commit_sha)

    return _wrap(run, kwargs)


def ghr_get_tree(**kwargs: Any) -> Dict[str, Any]:
    """Return the repository tree for a branch.

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
        branch (str, optional): branch name (default branch when empty).
        recursive (bool, optional, default False): include nested entries.
    Returns:
        {"ok": True, "result": {"tree_sha", "truncated", "entries": [...]}}
    """
    from core.github_connector_rest import get_tree

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        if not repo:
            raise GithubRestError("Missing required argument: repo")
        return get_tree(
            conn_id, repo,
            branch=str(kwargs.get("branch") or ""),
            recursive=bool(kwargs.get("recursive", False)),
        )

    return _wrap(run, kwargs)


def ghr_batch_commit(**kwargs: Any) -> Dict[str, Any]:
    """Publish many files in ONE commit via the Git Data API (optimized).

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
        files (list|str, required): [{"path", "content"}] or JSON string.
        message (str, optional): commit message.
        branch (str, optional): target branch (created when missing).
    Returns:
        {"ok": True, "result": {"commit_sha", "tree_sha", "total_files",
        "files_created", "files_updated", "files_unchanged", "committed",
        "ref_created", "ref_updated", ...}}
    """
    from core.github_connector_rest import batch_commit

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        if not repo:
            raise GithubRestError("Missing required argument: repo")
        files = _files_arg(kwargs, "ghr_batch_commit")
        return batch_commit(
            conn_id, repo, files,
            message=str(kwargs.get("message") or ""),
            branch=str(kwargs.get("branch") or ""),
        )

    return _wrap(run, kwargs)


def ghr_batch_upsert(**kwargs: Any) -> Dict[str, Any]:
    """Batch create/update by blob-SHA diff; skips unchanged files.

    Arguments:
        connector_id (str, required): connection id.
        repo (str, required): "owner/repo" or bare repo name.
        files (list|str, required): [{"path", "content"}] or JSON string.
        message (str, optional): commit message.
        branch (str, optional): target branch.
    Returns:
        {"ok": True, "result": {"commit_sha", "total_files", "files_created",
        "files_updated", "files_unchanged", "committed", ...}}
    """
    from core.github_connector_rest import batch_upsert

    def run():
        conn_id = _get_connector_id(kwargs)
        repo = str(kwargs.get("repo") or "").strip()
        if not repo:
            raise GithubRestError("Missing required argument: repo")
        files = _files_arg(kwargs, "ghr_batch_upsert")
        return batch_upsert(
            conn_id, repo, files,
            message=str(kwargs.get("message") or ""),
            branch=str(kwargs.get("branch") or ""),
        )

    return _wrap(run, kwargs)


# Tool metadata for orchestrator catalogs: name -> description.
TOOLS = {}
TOOLS["ghr_list_repos"] = {
    "name": "ghr_list_repos",
    "desc": (
        "List GitHub repositories of the authenticated user (REST connector). "
        "Arguments: connector_id (required), sort (optional)."
    ),
}
TOOLS["ghr_create_repo"] = {
    "name": "ghr_create_repo",
    "desc": (
        "Create a new GitHub repository (REST connector). "
        "Arguments: connector_id (required), name (required), description, private."
    ),
}
TOOLS["ghr_read_file"] = {
    "name": "ghr_read_file",
    "desc": (
        "Read a text file from a GitHub repository (REST connector). "
        "Arguments: connector_id, repo, path, branch."
    ),
}
TOOLS["ghr_upload_file"] = {
    "name": "ghr_upload_file",
    "desc": (
        "Upload (create) a NEW file in a GitHub repository (REST connector). "
        "Arguments: connector_id, repo, path, content, message, branch."
    ),
}
TOOLS["ghr_update_file"] = {
    "name": "ghr_update_file",
    "desc": (
        "Update an existing file in a GitHub repository (REST connector). "
        "Arguments: connector_id, repo, path, content, message, branch, sha."
    ),
}
TOOLS["ghr_delete_file"] = {
    "name": "ghr_delete_file",
    "desc": (
        "Delete a file from a GitHub repository (REST connector). "
        "Arguments: connector_id, repo, path, message, branch, sha."
    ),
}
TOOLS["ghr_list_files"] = {
    "name": "ghr_list_files",
    "desc": (
        "List top-level entries of a repository directory (REST connector). "
        "Arguments: connector_id, repo, path, branch."
    ),
}
TOOLS["ghr_test_connection"] = {
    "name": "ghr_test_connection",
    "desc": (
        "Validate a GitHub REST connection and refresh its account info. "
        "Arguments: connector_id (required)."
    ),
}
TOOLS["ghr_get_repo_info"] = {
    "name": "ghr_get_repo_info",
    "desc": (
        "Return metadata for one repository (REST connector). "
        "Arguments: connector_id (required), repo (required)."
    ),
}
TOOLS["ghr_read_file_meta"] = {
    "name": "ghr_read_file_meta",
    "desc": (
        "Return file metadata sha/size without content (REST connector). "
        "Arguments: connector_id, repo, path, branch."
    ),
}
TOOLS["ghr_get_ref"] = {
    "name": "ghr_get_ref",
    "desc": (
        "Return a branch head ref (REST connector). "
        "Arguments: connector_id, repo, branch, resolve."
    ),
}
TOOLS["ghr_get_commit"] = {
    "name": "ghr_get_commit",
    "desc": (
        "Return a Git commit object by SHA (REST connector). "
        "Arguments: connector_id, repo, commit_sha."
    ),
}
TOOLS["ghr_get_tree"] = {
    "name": "ghr_get_tree",
    "desc": (
        "Return the repository tree for a branch (REST connector). "
        "Arguments: connector_id, repo, branch, recursive."
    ),
}
TOOLS["ghr_batch_commit"] = {
    "name": "ghr_batch_commit",
    "desc": (
        "Publish many files in ONE commit via the Git Data API. "
        "Arguments: connector_id, repo, files ([{path, content}] or JSON), message, branch."
    ),
}
TOOLS["ghr_batch_upsert"] = {
    "name": "ghr_batch_upsert",
    "desc": (
        "Batch create/update by blob-SHA diff (skips unchanged files). "
        "Arguments: connector_id, repo, files ([{path, content}] or JSON), message, branch."
    ),
}


def get_tools() -> list:
    """Return metadata for all REST GitHub tools (for orchestrator catalogs)."""
    return [TOOLS[name] for name in sorted(TOOLS)]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
