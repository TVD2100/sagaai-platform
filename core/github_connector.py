# -*- coding: utf-8 -*-
"""
core.github_connector - thin GitHub adapter based on PyGithub.

All functions in this module operate on a connection id (see
``core.connectors``) so tokens never travel in plain text through the
application. Every function resolves the encrypted token, validates it,
and returns plain dict results suited for the orchestrator tools.

No streamlit imports. Errors raise ValueError with a user-facing message.
PyGithub is imported lazily; a missing dependency raises a clean
ValueError with installation hints instead of an ImportError.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core import connectors

__test__ = False  # pytest: functions named test_* are API, not unit tests


class GithubConnectorError(ValueError):
    """User-facing error raised by GitHub connector operations."""


def _ensure_github():
    """Import PyGithub lazily; return the github module or raise a clean error."""
    try:
        import github  # noqa: F401
        return github
    except ImportError:
        raise GithubConnectorError(
            "PyGithub is not installed. Run: pip install PyGithub"
        )


def _client(conn_id: str):
    """Build a PyGithub client for the given connection id."""
    github = _ensure_github()
    token = connectors.decrypt_token(conn_id)
    return github.Github(token)


def _repo_spec(gh, repo: str):
    """Resolve a repo specification to a PyGithub Repository object.

    Accepts "repo" (owned by the authenticated user) or "owner/repo".
    Returns (repository, owner, repo_name).
    """
    repo = (repo or "").strip()
    if not repo:
        raise GithubConnectorError("Repository name cannot be empty")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        owner = owner.strip()
        name = name.strip()
        if not owner or not name:
            raise GithubConnectorError("Invalid repository format; use 'owner/repo'")
        try:
            return gh.get_repo(f"{owner}/{name}"), owner, name
        except Exception as e:
            raise GithubConnectorError(f"Repository not found or not accessible: {owner}/{name}: {e}")
    # Bare repo name: resolve against the authenticated user.
    try:
        user = gh.get_user()
        login = user.login
        return gh.get_repo(f"{login}/{repo}"), login, repo
    except GithubConnectorError:
        raise
    except Exception as e:
        raise GithubConnectorError(f"Repository not found or not accessible: {repo}: {e}")


def _describe_gh_error(e: Exception) -> str:
    """Return a concise user-facing message for a PyGithub exception."""
    github = _ensure_github()
    if isinstance(e, github.GithubException):
        status = getattr(e, "status", None)
        data = getattr(e, "data", None)
        msg = ""
        if isinstance(data, dict):
            msg = data.get("message", "") or ""
        if status == 401:
            return "GitHub authentication failed (invalid or expired token)"
        if status == 403:
            return "GitHub access forbidden (check token permissions or rate limits)"
        if status == 404:
            return f"GitHub resource not found: {msg or e}"
        if msg:
            return f"GitHub API error ({status}): {msg}"
        return f"GitHub API error: {e}"
    return str(e)


def test_connection(conn_id: str) -> Dict[str, Any]:
    """Validate a connection against GitHub and refresh its account info.

    Calls GET /user and stores the login in the connection manifest.
    Returns {"ok": True, "login": ..., "name": ..., ...} on success.
    Raises GithubConnectorError on failure.
    """
    try:
        gh = _client(conn_id)
        user = gh.get_user()
    except GithubConnectorError:
        raise
    except Exception as e:
        raise GithubConnectorError(f"Connection failed: {_describe_gh_error(e)}")
    login = getattr(user, "login", "") or ""
    display = getattr(user, "name", "") or login
    try:
        connectors.update_connection(conn_id, account=login)
    except Exception:
        pass  # account refresh is best-effort
    return {
        "ok": True,
        "login": login,
        "name": display,
        "id": getattr(user, "id", None),
        "html_url": getattr(user, "html_url", "") or "",
    }


def get_user_info(conn_id: str) -> Dict[str, Any]:
    """Return profile info for the authenticated user."""
    try:
        gh = _client(conn_id)
        user = gh.get_user()
    except GithubConnectorError:
        raise
    except Exception as e:
        raise GithubConnectorError(f"Cannot fetch user profile: {_describe_gh_error(e)}")
    return {
        "login": getattr(user, "login", "") or "",
        "name": getattr(user, "name", "") or "",
        "email": getattr(user, "email", "") or "",
        "public_repos": getattr(user, "public_repos", 0) or 0,
        "html_url": getattr(user, "html_url", "") or "",
    }


def list_repos(conn_id: str, sort: str = "updated") -> List[Dict[str, Any]]:
    """List repositories accessible to the authenticated user.

    Returns a list of {"full_name", "name", "private", "description",
    "html_url", "default_branch"}.
    """
    try:
        gh = _client(conn_id)
        repos = gh.get_user().get_repos(sort=sort)
    except GithubConnectorError:
        raise
    except Exception as e:
        raise GithubConnectorError(f"Cannot list repositories: {_describe_gh_error(e)}")
    result = []
    for repo in repos:
        result.append({
            "full_name": getattr(repo, "full_name", "") or "",
            "name": getattr(repo, "name", "") or "",
            "private": bool(getattr(repo, "private", False)),
            "description": getattr(repo, "description", "") or "",
            "html_url": getattr(repo, "html_url", "") or "",
            "default_branch": getattr(repo, "default_branch", "main") or "main",
        })
    return result


def get_repo_info(conn_id: str, repo: str) -> Dict[str, Any]:
    """Return metadata for one repository ("repo" or "owner/repo")."""
    try:
        gh = _client(conn_id)
        repository, owner, name = _repo_spec(gh, repo)
    except GithubConnectorError:
        raise
    except Exception as e:
        raise GithubConnectorError(f"Cannot fetch repository: {_describe_gh_error(e)}")
    return {
        "full_name": getattr(repository, "full_name", f"{owner}/{name}"),
        "name": getattr(repository, "name", name),
        "owner": owner,
        "private": bool(getattr(repository, "private", False)),
        "description": getattr(repository, "description", "") or "",
        "html_url": getattr(repository, "html_url", "") or "",
        "default_branch": getattr(repository, "default_branch", "main") or "main",
    }


def create_repo(conn_id: str, name: str, description: str = "",
                private: bool = True, auto_init: bool = True) -> Dict[str, Any]:
    """Create a new repository under the authenticated user.

    Returns {"full_name", "name", "html_url", ...}. GitHub requires the
    repository name to not contain uppercase letters or spaces.
    """
    name = (name or "").strip()
    if not name:
        raise GithubConnectorError("Repository name cannot be empty")
    try:
        gh = _client(conn_id)
        user = gh.get_user()
        repo = user.create_repo(
            name=name,
            description=description or "",
            private=bool(private),
            auto_init=bool(auto_init),
        )
    except GithubConnectorError:
        raise
    except Exception as e:
        raise GithubConnectorError(f"Cannot create repository: {_describe_gh_error(e)}")
    return {
        "full_name": getattr(repo, "full_name", ""),
        "name": getattr(repo, "name", name),
        "html_url": getattr(repo, "html_url", "") or "",
        "default_branch": getattr(repo, "default_branch", "main") or "main",
    }


def read_file(conn_id: str, repo: str, path: str, branch: str = "") -> Dict[str, Any]:
    """Read a text file from a repository via the Contents API.

    Returns {"path", "content", "sha", "url"}. Content is UTF-8 decoded.
    """
    path = (path or "").strip().lstrip("/")
    if not path:
        raise GithubConnectorError("File path cannot be empty")
    try:
        gh = _client(conn_id)
        repository, _owner, _name = _repo_spec(gh, repo)
        kwargs = {}
        if branch:
            kwargs["ref"] = branch
        content_file = repository.get_contents(path, **kwargs)
    except GithubConnectorError:
        raise
    except Exception as e:
        raise GithubConnectorError(f"Cannot read file: {_describe_gh_error(e)}")
    content = getattr(content_file, "content", "") or ""
    try:
        import base64
        decoded = base64.b64decode(content).decode("utf-8")
    except Exception:
        decoded = content
    return {
        "path": getattr(content_file, "path", path),
        "content": decoded,
        "sha": getattr(content_file, "sha", "") or "",
        "url": getattr(content_file, "html_url", "") or "",
    }


def upload_file(conn_id: str, repo: str, path: str, content: str,
                message: str = "", branch: str = "") -> Dict[str, Any]:
    """Create a new file in a repository.

    Fails with a clean error when the file already exists (GitHub requires
    the file SHA to update; use ``update_file`` instead). Returns
    {"path", "sha", "committed"}.
    """
    path = (path or "").strip().lstrip("/")
    if not path:
        raise GithubConnectorError("File path cannot be empty")
    content = content or ""
    try:
        gh = _client(conn_id)
        repository, _owner, _name = _repo_spec(gh, repo)
        kwargs = {"path": path, "message": message or f"Add {path}", "content": content}
        if branch:
            kwargs["branch"] = branch
        result = repository.create_file(**kwargs)
    except GithubConnectorError:
        raise
    except Exception as e:
        msg = _describe_gh_error(e)
        if _is_conflict(e, "exists"):
            raise GithubConnectorError(
                f"File already exists and does not match: {path}. Use update_file to change it."
            )
        raise GithubConnectorError(f"Cannot upload file: {msg}")
    commit = result.get("commit", {}) if isinstance(result, dict) else {}
    return {
        "path": path,
        "sha": _content_sha(result),
        "committed": True,
        "commit_sha": (commit or {}).get("sha", "") if isinstance(commit, dict) else "",
    }


def update_file(conn_id: str, repo: str, path: str, content: str,
                message: str = "", branch: str = "", sha: str = "") -> Dict[str, Any]:
    """Update an existing file in a repository.

    When *sha* is omitted the current SHA is fetched first. Returns
    {"path", "sha", "committed"}.
    """
    path = (path or "").strip().lstrip("/")
    if not path:
        raise GithubConnectorError("File path cannot be empty")
    content = content or ""
    try:
        gh = _client(conn_id)
        repository, _owner, _name = _repo_spec(gh, repo)
        if not sha:
            current = read_file(conn_id, repo, path, branch=branch)
            sha = current.get("sha", "")
        kwargs = {"path": path, "message": message or f"Update {path}", "content": content, "sha": sha}
        if branch:
            kwargs["branch"] = branch
        result = repository.update_file(**kwargs)
    except GithubConnectorError:
        raise
    except Exception as e:
        raise GithubConnectorError(f"Cannot update file: {_describe_gh_error(e)}")
    commit = result.get("commit", {}) if isinstance(result, dict) else {}
    return {
        "path": path,
        "sha": _content_sha(result),
        "committed": True,
        "commit_sha": (commit or {}).get("sha", "") if isinstance(commit, dict) else "",
    }


def delete_file(conn_id: str, repo: str, path: str,
                message: str = "", branch: str = "", sha: str = "") -> Dict[str, Any]:
    """Delete a file from a repository.

    Returns {"path", "committed"}.
    """
    path = (path or "").strip().lstrip("/")
    if not path:
        raise GithubConnectorError("File path cannot be empty")
    try:
        gh = _client(conn_id)
        repository, _owner, _name = _repo_spec(gh, repo)
        if not sha:
            current = read_file(conn_id, repo, path, branch=branch)
            sha = current.get("sha", "")
        kwargs = {"path": path, "message": message or f"Delete {path}", "sha": sha}
        if branch:
            kwargs["branch"] = branch
        result = repository.delete_file(**kwargs)
    except GithubConnectorError:
        raise
    except Exception as e:
        raise GithubConnectorError(f"Cannot delete file: {_describe_gh_error(e)}")
    return {"path": path, "committed": True}


def list_files(conn_id: str, repo: str, path: str = "",
               branch: str = "") -> List[Dict[str, Any]]:
    """List the top-level entries of a repository directory.

    Returns a list of {"name", "path", "type"} ("file" | "dir").
    """
    path = (path or "").strip().lstrip("/")
    try:
        gh = _client(conn_id)
        repository, _owner, _name = _repo_spec(gh, repo)
        kwargs = {}
        if branch:
            kwargs["ref"] = branch
        contents = repository.get_contents(path, **kwargs)
    except GithubConnectorError:
        raise
    except Exception as e:
        raise GithubConnectorError(f"Cannot list directory: {_describe_gh_error(e)}")
    if not isinstance(contents, list):
        contents = [contents]
    result = []
    for item in contents:
        item_type = getattr(item, "type", "file") or "file"
        result.append({
            "name": getattr(item, "name", "") or "",
            "path": getattr(item, "path", "") or "",
            "type": "dir" if item_type == "dir" else "file",
        })
    return result


def _content_sha(result: Any) -> str:
    """Extract the content SHA from a create_file/update_file result."""
    if isinstance(result, dict):
        content = result.get("content", {})
        if isinstance(content, dict):
            return content.get("sha", "") or ""
        if hasattr(content, "sha"):
            return getattr(content, "sha", "") or ""
        return ""
    if hasattr(result, "sha"):
        return getattr(result, "sha", "") or ""
    return ""


def _is_conflict(e: Exception, marker: str) -> bool:
    """Return True when the PyGithub exception looks like a conflict."""
    github = _ensure_github()
    if not isinstance(e, github.GithubException):
        return False
    if getattr(e, "status", None) == 422:
        return "".join([str(v) for v in (getattr(e, "data", {}) or {}).values()]).lower().find(marker) >= 0
    return False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
