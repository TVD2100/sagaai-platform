# -*- coding: utf-8 -*-
"""
core.github_connector_rest - direct GitHub REST API v3 connector (requests).

A PyGithub-free alternative to ``core.github_connector``. It implements the
same user-facing operations over plain HTTP (REST API v3, api version
``2022-11-28``) and adds optimized batch publishing for large file sets via
the Git Data API: all blobs are created first, then ONE tree, ONE commit and
ONE ref update are performed for the whole batch (the Contents API would
create one commit per file).

All functions operate on a connection id (see ``core.connectors``) so tokens
never travel in plain text through the application. The encrypted token is
resolved and decrypted inside this module and is never included in results
or error messages.

No streamlit imports. Errors raise ``GithubRestError`` (a ValueError
subclass) with a user-facing message. ``requests`` is imported lazily; a
missing dependency raises a clean error with installation hints.

Batch pipeline (batch_commit / batch_upsert):
    1. POST /repos/{owner}/{repo}/git/blobs        (one call per new blob)
    2. POST /repos/{owner}/{repo}/git/trees        (one call per <=9000 entries)
    3. POST /repos/{owner}/{repo}/git/commits      (ONE commit for all files)
    4. PATCH /repos/{owner}/{repo}/git/refs/heads/{branch}
       (or POST /git/refs when the branch does not exist yet)
"""
from __future__ import annotations

import base64
import hashlib
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from core import connectors

__test__ = False  # pytest: functions named test_* are API, not unit tests

API_BASE: str = "https://api.github.com"
API_VERSION_HEADER: str = "2022-11-28"
_DEFAULT_TIMEOUT: float = 30.0
_BATCH_TIMEOUT: float = 120.0
_TREE_CREATE_CHUNK: int = 9000  # GitHub: max 10000 entries per tree creation
_LIST_PER_PAGE: int = 100

_session_cache: Dict[str, Any] = {}


class GithubRestError(ValueError):
    """User-facing error raised by REST GitHub connector operations.

    Carries an optional ``status`` (HTTP status code) so callers can detect
    concrete failure classes (e.g. 404 for a missing branch).
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _ensure_requests():
    """Import requests lazily; raise a clean error when it is missing."""
    try:
        import requests  # noqa: F401
        return requests
    except ImportError:
        raise GithubRestError(
            "requests is not installed. Run: pip install requests"
        )


def _session(api_base: str):
    """Return a cached requests.Session configured for the GitHub API."""
    _ensure_requests()
    if api_base not in _session_cache:
        import requests
        sess = requests.Session()
        sess.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION_HEADER,
        })
        _session_cache[api_base] = sess
    return _session_cache[api_base]


def _api_base(conn_id: str) -> str:
    """Return the API base URL for a connection (manifest override or default)."""
    data = connectors.get_connection_full(conn_id) or {}
    base = str(data.get("api_base") or "").strip()
    return (base or API_BASE).rstrip("/")


def _describe_rest_error(status: int, data: Any) -> str:
    """Return a concise user-facing message for an HTTP error response."""
    msg = ""
    if isinstance(data, dict):
        msg = str(data.get("message") or "").strip()
    if status == 401:
        return "GitHub authentication failed (invalid or expired token)"
    if status == 403:
        base_msg = "GitHub access forbidden (check token permissions or rate limits)"
        return f"{base_msg}: {msg}" if msg else base_msg
    if status == 404:
        return f"GitHub resource not found: {msg}" if msg else "GitHub resource not found"
    if msg:
        return f"GitHub API error ({status}): {msg}"
    return f"GitHub API error ({status})"


def _request(conn_id: str, method: str, url_path: str,
             body: Optional[Dict[str, Any]] = None,
             params: Optional[Dict[str, Any]] = None,
             timeout: float = _DEFAULT_TIMEOUT) -> Any:
    """Perform one authenticated GitHub REST call and return parsed JSON.

    204 responses return None. Non-2xx raise ``GithubRestError`` with a
    mapped message; the token is never included in errors.
    """
    _ensure_requests()
    api = _api_base(conn_id)
    token = connectors.decrypt_token(conn_id)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = _session(api).request(
            method,
            f"{api}{url_path}",
            json=body,
            params=params,
            headers=headers,
            timeout=timeout,
        )
    except Exception as e:  # requests.RequestException and friends
        raise GithubRestError(f"GitHub network error: {e}")
    if resp.status_code == 204:
        return None
    if resp.status_code >= 400:
        try:
            data: Any = resp.json()
        except Exception:
            text = (resp.text or "")[:200]
            data = {"message": text} if text else {}
        raise GithubRestError(_describe_rest_error(resp.status_code, data),
                              status=resp.status_code)
    try:
        return resp.json()
    except ValueError:
        return {}


def _quote_path(path: str) -> str:
    """URL-quote a repository path segment-by-segment."""
    return "/".join(
        urllib.parse.quote(seg, safe="")
        for seg in (path or "").lstrip("/").split("/")
    )


def _quote_branch(branch: str) -> str:
    """URL-quote a branch name (slashes separate ref path segments)."""
    return urllib.parse.quote(branch or "", safe="/")


def _repo_spec(conn_id: str, repo: str) -> Tuple[str, str]:
    """Resolve a repo specification ("repo" or "owner/repo") to (owner, name)."""
    repo = (repo or "").strip()
    if not repo:
        raise GithubRestError("Repository name cannot be empty")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        owner = owner.strip()
        name = name.strip()
        if not owner or not name:
            raise GithubRestError("Invalid repository format; use 'owner/repo'")
        return owner, name
    user = get_user_info(conn_id)
    login = str(user.get("login") or "").strip()
    if not login:
        raise GithubRestError("Cannot resolve the authenticated user login")
    return login, repo.strip()


def _default_branch(conn_id: str, owner: str, name: str) -> str:
    """Return the default branch of a repository ("main" fallback)."""
    try:
        data = _request(conn_id, "GET", f"/repos/{owner}/{name}")
    except GithubRestError as e:
        if e.status == 404:
            raise GithubRestError(f"Repository not found or not accessible: {owner}/{name}")
        raise
    return str((data or {}).get("default_branch") or "main")


def test_connection(conn_id: str) -> Dict[str, Any]:
    """Validate a connection against GitHub and refresh its account info.

    Calls GET /user and stores the login in the connection manifest.
    Returns {"ok": True, "login": ..., "name": ..., ...} on success.
    Raises GithubRestError on failure.
    """
    data = _request(conn_id, "GET", "/user")
    user = data if isinstance(data, dict) else {}
    login = str(user.get("login") or "")
    display = str(user.get("name") or login)
    if login:
        try:
            connectors.update_connection(conn_id, account=login)
        except Exception:
            pass  # account refresh is best-effort
    return {
        "ok": True,
        "login": login,
        "name": display,
        "id": user.get("id"),
        "html_url": str(user.get("html_url") or ""),
    }


def get_user_info(conn_id: str) -> Dict[str, Any]:
    """Return profile info for the authenticated user."""
    data = _request(conn_id, "GET", "/user")
    user = data if isinstance(data, dict) else {}
    return {
        "login": str(user.get("login") or ""),
        "name": str(user.get("name") or ""),
        "email": str(user.get("email") or ""),
        "public_repos": int(user.get("public_repos") or 0),
        "html_url": str(user.get("html_url") or ""),
    }


def list_repos(conn_id: str, sort: str = "updated") -> List[Dict[str, Any]]:
    """List repositories accessible to the authenticated user (paginated)."""
    result: List[Dict[str, Any]] = []
    page = 1
    while page <= 50:
        data = _request(
            conn_id, "GET", "/user/repos",
            params={
                "sort": sort or "updated",
                "per_page": _LIST_PER_PAGE,
                "page": page,
                "type": "all",
            },
        )
        items = data if isinstance(data, list) else []
        for repo in items:
            if not isinstance(repo, dict):
                continue
            result.append({
                "full_name": str(repo.get("full_name") or ""),
                "name": str(repo.get("name") or ""),
                "private": bool(repo.get("private", False)),
                "description": str(repo.get("description") or ""),
                "html_url": str(repo.get("html_url") or ""),
                "default_branch": str(repo.get("default_branch") or "main"),
            })
        if len(items) < _LIST_PER_PAGE:
            break
        page += 1
    return result


def get_repo_info(conn_id: str, repo: str) -> Dict[str, Any]:
    """Return metadata for one repository ("repo" or "owner/repo")."""
    owner, name = _repo_spec(conn_id, repo)
    data = _request(conn_id, "GET", f"/repos/{owner}/{name}")
    info = data if isinstance(data, dict) else {}
    return {
        "full_name": str(info.get("full_name") or f"{owner}/{name}"),
        "name": str(info.get("name") or name),
        "owner": str((info.get("owner") or {}).get("login") or owner),
        "private": bool(info.get("private", False)),
        "description": str(info.get("description") or ""),
        "html_url": str(info.get("html_url") or ""),
        "default_branch": str(info.get("default_branch") or "main"),
    }


def create_repo(conn_id: str, name: str, description: str = "",
                private: bool = True, auto_init: bool = True) -> Dict[str, Any]:
    """Create a new repository under the authenticated user.

    GitHub requires the repository name to not contain uppercase letters or
    spaces (client-side validation only; the API reports other violations).
    """
    name = (name or "").strip()
    if not name:
        raise GithubRestError("Repository name cannot be empty")
    data = _request(conn_id, "POST", "/user/repos", body={
        "name": name,
        "description": description or "",
        "private": bool(private),
        "auto_init": bool(auto_init),
    })
    info = data if isinstance(data, dict) else {}
    return {
        "full_name": str(info.get("full_name") or ""),
        "name": str(info.get("name") or name),
        "html_url": str(info.get("html_url") or ""),
        "default_branch": str(info.get("default_branch") or "main"),
        "private": bool(info.get("private", False)),
    }


def read_file_meta(conn_id: str, repo: str, path: str,
                   branch: str = "") -> Dict[str, Any]:
    """Return file metadata (sha, size) without the file content.

    Used as a cheap "does this file exist and what is its SHA" probe.
    Returns {"path", "sha", "size", "url"}.
    """
    owner, name = _repo_spec(conn_id, repo)
    path = (path or "").strip().lstrip("/")
    if not path:
        raise GithubRestError("File path cannot be empty")
    params: Dict[str, Any] = {}
    if branch:
        params["ref"] = branch
    data = _request(conn_id, "GET",
                    f"/repos/{owner}/{name}/contents/{_quote_path(path)}",
                    params=params)
    info = data if isinstance(data, dict) else {}
    return {
        "path": str(info.get("path") or path),
        "sha": str(info.get("sha") or ""),
        "size": int(info.get("size") or 0),
        "url": str(info.get("html_url") or ""),
    }


def read_file(conn_id: str, repo: str, path: str, branch: str = "") -> Dict[str, Any]:
    """Read a text file from a repository via the Contents API.

    Returns {"path", "content", "sha", "url"}. Content is UTF-8 decoded;
    binary-only encodings are returned as empty content.
    """
    owner, name = _repo_spec(conn_id, repo)
    path = (path or "").strip().lstrip("/")
    if not path:
        raise GithubRestError("File path cannot be empty")
    params: Dict[str, Any] = {}
    if branch:
        params["ref"] = branch
    data = _request(conn_id, "GET",
                    f"/repos/{owner}/{name}/contents/{_quote_path(path)}",
                    params=params)
    info = data if isinstance(data, dict) else {}
    raw = str(info.get("content") or "")
    if str(info.get("encoding") or "").lower() == "base64" and raw:
        try:
            decoded = base64.b64decode(raw).decode("utf-8")
        except Exception:
            decoded = ""
    else:
        decoded = ""
    return {
        "path": str(info.get("path") or path),
        "content": decoded,
        "sha": str(info.get("sha") or ""),
        "url": str(info.get("html_url") or ""),
    }


def upload_file(conn_id: str, repo: str, path: str, content: str,
                message: str = "", branch: str = "") -> Dict[str, Any]:
    """Create a NEW file in a repository (one commit via the Contents API).

    Fails with a clean error when the file already exists (use
    ``update_file`` instead). Returns {"path", "sha", "committed",
    "commit_sha"}.
    """
    owner, name = _repo_spec(conn_id, repo)
    path = (path or "").strip().lstrip("/")
    if not path:
        raise GithubRestError("File path cannot be empty")
    body: Dict[str, Any] = {
        "message": message or f"Add {path}",
        "content": base64.b64encode((content or "").encode("utf-8")).decode("ascii"),
    }
    if branch:
        body["branch"] = branch
    try:
        data = _request(conn_id, "PUT",
                        f"/repos/{owner}/{name}/contents/{_quote_path(path)}",
                        body=body)
    except GithubRestError as e:
        if e.status == 422:
            raise GithubRestError(
                f"File already exists and does not match: {path}. "
                "Use update_file to change it."
            )
        raise GithubRestError(f"Cannot upload file: {e}")
    info = data if isinstance(data, dict) else {}
    content_info = info.get("content") or {}
    commit = info.get("commit") or {}
    return {
        "path": path,
        "sha": str((content_info or {}).get("sha") or ""),
        "committed": True,
        "commit_sha": str((commit or {}).get("sha") or ""),
    }


def update_file(conn_id: str, repo: str, path: str, content: str,
                message: str = "", branch: str = "",
                sha: str = "") -> Dict[str, Any]:
    """Update an existing file (one commit via the Contents API).

    When *sha* is omitted the current SHA is fetched first. Returns
    {"path", "sha", "committed", "commit_sha"}.
    """
    owner, name = _repo_spec(conn_id, repo)
    path = (path or "").strip().lstrip("/")
    if not path:
        raise GithubRestError("File path cannot be empty")
    if not sha:
        sha = str(read_file_meta(conn_id, repo, path, branch=branch).get("sha") or "")
    body: Dict[str, Any] = {
        "message": message or f"Update {path}",
        "content": base64.b64encode((content or "").encode("utf-8")).decode("ascii"),
        "sha": sha,
    }
    if branch:
        body["branch"] = branch
    try:
        data = _request(conn_id, "PUT",
                        f"/repos/{owner}/{name}/contents/{_quote_path(path)}",
                        body=body)
    except GithubRestError as e:
        raise GithubRestError(f"Cannot update file: {e}")
    info = data if isinstance(data, dict) else {}
    content_info = info.get("content") or {}
    commit = info.get("commit") or {}
    return {
        "path": path,
        "sha": str((content_info or {}).get("sha") or ""),
        "committed": True,
        "commit_sha": str((commit or {}).get("sha") or ""),
    }


def delete_file(conn_id: str, repo: str, path: str,
                message: str = "", branch: str = "",
                sha: str = "") -> Dict[str, Any]:
    """Delete a file from a repository (one commit via the Contents API)."""
    owner, name = _repo_spec(conn_id, repo)
    path = (path or "").strip().lstrip("/")
    if not path:
        raise GithubRestError("File path cannot be empty")
    if not sha:
        sha = str(read_file_meta(conn_id, repo, path, branch=branch).get("sha") or "")
    body: Dict[str, Any] = {"message": message or f"Delete {path}", "sha": sha}
    if branch:
        body["branch"] = branch
    try:
        _request(conn_id, "DELETE",
                 f"/repos/{owner}/{name}/contents/{_quote_path(path)}",
                 body=body)
    except GithubRestError as e:
        raise GithubRestError(f"Cannot delete file: {e}")
    return {"path": path, "committed": True}


def list_files(conn_id: str, repo: str, path: str = "",
               branch: str = "") -> List[Dict[str, Any]]:
    """List the top-level entries of a repository directory.

    Returns a list of {"name", "path", "type"} ("file" | "dir").
    """
    owner, name = _repo_spec(conn_id, repo)
    path = (path or "").strip().lstrip("/")
    params: Dict[str, Any] = {}
    if branch:
        params["ref"] = branch
    try:
        data = _request(conn_id, "GET",
                        f"/repos/{owner}/{name}/contents/{_quote_path(path)}",
                        params=params)
    except GithubRestError:
        raise GithubRestError("Cannot list directory")
    items = data if isinstance(data, list) else ([data] if data else [])
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result.append({
            "name": str(item.get("name") or ""),
            "path": str(item.get("path") or ""),
            "type": "dir" if item.get("type") == "dir" else "file",
        })
    return result


def get_ref(conn_id: str, repo: str, branch: str = "",
            resolve: bool = True) -> Dict[str, Any]:
    """Return a ref (usually a branch head) as {"ref", "sha", "object_type"}.

    When *branch* is empty the repository default branch is used. When
    *resolve* is False the ref is returned without resolving the commit.
    """
    owner, name = _repo_spec(conn_id, repo)
    branch = branch or _default_branch(conn_id, owner, name)
    data = _request(conn_id, "GET",
                    f"/repos/{owner}/{name}/git/ref/heads/{_quote_branch(branch)}")
    info = data if isinstance(data, dict) else {}
    obj = info.get("object") or {}
    return {
        "ref": str(info.get("ref") or f"refs/heads/{branch}"),
        "sha": str((obj or {}).get("sha") or ""),
        "object_type": str((obj or {}).get("type") or ""),
    }


def get_commit(conn_id: str, repo: str, commit_sha: str) -> Dict[str, Any]:
    """Return a Git commit object: {"sha", "message", "tree_sha", "parents"}."""
    owner, name = _repo_spec(conn_id, repo)
    if not (commit_sha or "").strip():
        raise GithubRestError("Commit SHA cannot be empty")
    data = _request(conn_id, "GET",
                    f"/repos/{owner}/{name}/git/commits/{commit_sha.strip()}")
    info = data if isinstance(data, dict) else {}
    return {
        "sha": str(info.get("sha") or ""),
        "message": str(info.get("message") or ""),
        "tree_sha": str((info.get("tree") or {}).get("sha") or ""),
        "parents": [
            str((p or {}).get("sha") or "")
            for p in (info.get("parents") or [])
            if isinstance(p, dict)
        ],
    }


def get_tree(conn_id: str, repo: str, branch: str = "",
             recursive: bool = False) -> Dict[str, Any]:
    """Return the repository tree for a branch.

    Returns {"tree_sha", "truncated", "entries": [{"path", "type", "sha",
    "mode", "size"}]}. Entry ``size`` is present only for blobs.
    """
    ref = get_ref(conn_id, repo, branch=branch)
    target = ref.get("sha") or ""
    if not target:
        raise GithubRestError("Cannot resolve the target branch")
    commit = get_commit(conn_id, repo, target)
    tree_sha = commit.get("tree_sha") or ""
    if not tree_sha:
        raise GithubRestError("Cannot resolve the commit tree")
    owner, name = _repo_spec(conn_id, repo)
    params: Dict[str, Any] = {}
    if recursive:
        params["recursive"] = "1"
    data = _request(conn_id, "GET",
                    f"/repos/{owner}/{name}/git/trees/{tree_sha}",
                    params=params)
    info = data if isinstance(data, dict) else {}
    entries = []
    for item in info.get("tree") or []:
        if not isinstance(item, dict):
            continue
        entry = {
            "path": str(item.get("path") or ""),
            "type": str(item.get("type") or "blob"),
            "sha": str(item.get("sha") or ""),
            "mode": str(item.get("mode") or ""),
        }
        if item.get("size") is not None:
            entry["size"] = int(item.get("size"))
        entries.append(entry)
    return {
        "tree_sha": str(info.get("sha") or tree_sha),
        "truncated": bool(info.get("truncated", False)),
        "entries": entries,
    }


def _git_blob_sha(content: str) -> str:
    """Compute the canonical Git blob SHA-1 for UTF-8 text content."""
    data = (content or "").encode("utf-8")
    header = b"blob " + str(len(data)).encode("ascii") + b"\x00"
    return hashlib.sha1(header + data).hexdigest()


def _create_blob(conn_id: str, owner: str, name: str, content: str) -> str:
    """Create one Git blob (UTF-8 plain content) and return its SHA."""
    try:
        data = _request(
            conn_id, "POST", f"/repos/{owner}/{name}/git/blobs",
            body={"content": content or "", "encoding": "utf-8"},
            timeout=_BATCH_TIMEOUT,
        )
    except GithubRestError as e:
        if e.status == 409 and "empty" in str(e).lower():
            raise GithubRestError(
                "GitHub repository is empty: the Git Data API becomes available "
                "only after the first commit. Create the repository with "
                "auto_init=True, or upload the first file via upload_file "
                "before using batch operations.",
                status=409,
            )
        raise
    info = data if isinstance(data, dict) else {}
    blob_sha = str(info.get("sha") or "")
    if not blob_sha:
        raise GithubRestError("GitHub did not return a blob SHA")
    return blob_sha


def _create_tree_chain(conn_id: str, owner: str, name: str,
                       base_tree: str, entries: List[Dict[str, str]]) -> str:
    """Create tree(s) for *entries*, chunked to GitHub's per-call limit.

    Returns the SHA of the final tree. When *entries* is empty and a
    *base_tree* exists, the base tree SHA is returned unchanged.
    """
    if not entries:
        if base_tree:
            return base_tree
        raise GithubRestError("Cannot create a commit without file entries")
    current = base_tree or ""
    for i in range(0, len(entries), _TREE_CREATE_CHUNK):
        chunk = entries[i:i + _TREE_CREATE_CHUNK]
        body: Dict[str, Any] = {"tree": chunk}
        if current:
            body["base_tree"] = current
        data = _request(
            conn_id, "POST", f"/repos/{owner}/{name}/git/trees",
            body=body, timeout=_BATCH_TIMEOUT,
        )
        info = data if isinstance(data, dict) else {}
        current = str(info.get("sha") or "")
        if not current:
            raise GithubRestError("GitHub did not return a tree SHA")
    return current


def _resolve_target_ref(conn_id: str, owner: str, name: str,
                        branch: str) -> Tuple[str, Optional[str], Optional[str], bool]:
    """Resolve the target branch into (branch, parent_sha, base_tree, exists).

    For an existing branch returns its head commit SHA and tree SHA. For a
    repository without the branch yet returns (branch, None, None, False).
    """
    branch = branch or _default_branch(conn_id, owner, name)
    parent: Optional[str] = None
    base_tree: Optional[str] = None
    exists = False
    try:
        data = _request(
            conn_id, "GET",
            f"/repos/{owner}/{name}/git/ref/heads/{_quote_branch(branch)}"
        )
    except GithubRestError as e:
        if e.status == 404:
            pass  # branch not found yet: it will be created during publish
        elif e.status == 409 and "empty" in str(e).lower():
            pass  # no commits yet: GitHub answers 409 "Git Repository is empty"
        else:
            raise
    else:
        exists = True
        info = data if isinstance(data, dict) else {}
        parent = str((info.get("object") or {}).get("sha") or "") or None
    if parent:
        commit = get_commit(conn_id, f"{owner}/{name}", parent)
        base_tree = commit.get("tree_sha") or None
    return branch, parent, base_tree, exists


def _publish_commit(conn_id: str, owner: str, name: str, branch: str,
                    parent: Optional[str], base_tree: Optional[str],
                    tree_entries: List[Dict[str, str]],
                    message: str) -> Dict[str, Any]:
    """Perform tree+commit+ref-update for a batch and return a summary."""
    tree_sha = _create_tree_chain(conn_id, owner, name, base_tree or "", tree_entries)
    commit_body: Dict[str, Any] = {
        "message": message or f"Batch commit ({len(tree_entries)} files)",
        "tree": tree_sha,
    }
    if parent:
        commit_body["parents"] = [parent]
    data = _request(
        conn_id, "POST", f"/repos/{owner}/{name}/git/commits",
        body=commit_body, timeout=_BATCH_TIMEOUT,
    )
    info = data if isinstance(data, dict) else {}
    commit_sha = str(info.get("sha") or "")
    if not commit_sha:
        raise GithubRestError("GitHub did not return a commit SHA")
    ref_created = False
    ref_updated = False
    if parent is not None:
        _request(conn_id, "PATCH",
                 f"/repos/{owner}/{name}/git/refs/heads/{_quote_branch(branch)}",
                 body={"sha": commit_sha, "force": False},
                 timeout=_BATCH_TIMEOUT)
        ref_updated = True
    else:
        _request(conn_id, "POST", f"/repos/{owner}/{name}/git/refs",
                 body={"ref": f"refs/heads/{branch}", "sha": commit_sha},
                 timeout=_BATCH_TIMEOUT)
        ref_created = True
    return {
        "branch": branch,
        "ref": f"refs/heads/{branch}",
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "ref_created": ref_created,
        "ref_updated": ref_updated,
    }


def _normalize_batch_files(files: Any) -> List[Dict[str, Any]]:
    """Validate and normalize the ``files`` argument of batch operations.

    Every entry must be a dict with a non-empty ``path`` and either
    ``content`` (text) or ``blob_sha``. Duplicate paths are rejected.
    """
    if not isinstance(files, (list, tuple)) or not files:
        raise GithubRestError("Batch files list cannot be empty")
    result: List[Dict[str, Any]] = []
    seen = set()
    for item in files:
        if not isinstance(item, dict):
            raise GithubRestError("Each batch file entry must be a dict {path, content}")
        path = str(item.get("path") or "").strip().lstrip("/")
        if not path:
            raise GithubRestError("File path cannot be empty")
        if path in seen:
            raise GithubRestError(f"Duplicate path in batch: {path}")
        seen.add(path)
        has_content = item.get("content") is not None
        blob_sha = str(item.get("blob_sha") or "").strip()
        if not has_content and not blob_sha:
            raise GithubRestError(
                f"Batch entry for '{path}' needs 'content' or 'blob_sha'"
            )
        result.append({
            "path": path,
            "content": str(item.get("content")) if has_content else "",
            "blob_sha": blob_sha if not has_content else "",
            "mode": str(item.get("mode") or "100644"),
        })
    return result


def batch_commit(conn_id: str, repo: str, files: Any,
                 message: str = "", branch: str = "") -> Dict[str, Any]:
    """Publish many files in ONE commit via the Git Data API.

    ``files`` is a list of {"path", "content"} (or {"path", "blob_sha"})
    dictionaries. All blobs are created first, then one tree, one commit
    and one ref update are performed. Creates the branch when the
    repository has no commits yet.

    Note: a completely empty repository rejects Git Data API calls until
    its first commit exists (GitHub answers 409 "Git Repository is empty").
    Seed such a repository with ``upload_file`` or create it with
    ``auto_init=True`` before batch publishing.

    Returns {"branch", "ref", "commit_sha", "tree_sha", "files_created",
    "files_updated", "files_unchanged", "total_files", "committed",
    "ref_created", "ref_updated"}.
    """
    owner, name = _repo_spec(conn_id, repo)
    branch, parent, base_tree, _exists = _resolve_target_ref(conn_id, owner, name, branch)
    entries = _normalize_batch_files(files)
    # Total for reporting (before unchanged-filtering in batch_commit there is
    # no filtering: every entry is published).
    blob_map: Dict[str, str] = {}
    for entry in entries:
        blob_map[entry["path"]] = entry["blob_sha"] or _create_blob(
            conn_id, owner, name, entry["content"]
        )
    tree_entries: List[Dict[str, str]] = [
        {"path": e["path"], "mode": e["mode"], "type": "blob", "sha": blob_map[e["path"]]}
        for e in entries
    ]
    summary = _publish_commit(conn_id, owner, name, branch, parent, base_tree,
                              tree_entries, message)
    summary.update({
        "files_created": 0,
        "files_updated": 0,
        "files_unchanged": 0,
        "total_files": len(entries),
        "committed": True,
    })
    # batch_commit does not diff against the remote tree: all entries are
    # written (created or effectively replaced) in the single commit.
    summary["files_created"] = len(entries)
    return summary


def batch_upsert(conn_id: str, repo: str, files: Any,
                 message: str = "", branch: str = "") -> Dict[str, Any]:
    """Batch create/update with content diffing (skip unchanged files).

    Takes a snapshot of the existing tree and for every entry compares the
    local Git blob SHA with the remote one:
      - identical blob already in the tree  -> skipped (no blob created);
      - path missing                         -> created;
      - path present with a different SHA    -> updated.

    When every file is unchanged no commit is performed and ``committed``
    is False. Otherwise one commit is created for all changed files.
    """
    owner, name = _repo_spec(conn_id, repo)
    branch, parent, base_tree, _exists = _resolve_target_ref(conn_id, owner, name, branch)
    entries = _normalize_batch_files(files)
    # Snapshot of remote blobs: path -> blob sha.
    remote_blobs: Dict[str, str] = {}
    if base_tree:
        tree_data = _request(
            conn_id, "GET",
            f"/repos/{owner}/{name}/git/trees/{base_tree}",
            params={"recursive": "1"},
            timeout=_BATCH_TIMEOUT,
        )
        for item in (tree_data or {}).get("tree") or []:
            if isinstance(item, dict) and item.get("type") == "blob":
                remote_blobs[str(item.get("path") or "")] = str(item.get("sha") or "")
    created = 0
    updated = 0
    unchanged = 0
    tree_entries: List[Dict[str, str]] = []
    for entry in entries:
        path = entry["path"]
        local_sha = entry["blob_sha"] or _git_blob_sha(entry["content"])
        remote_sha = remote_blobs.get(path, "")
        if remote_sha and remote_sha == local_sha:
            unchanged += 1
            continue
        if entry["blob_sha"] and not entry["content"] and entry["blob_sha"] == remote_sha:
            unchanged += 1
            continue
        blob_sha = entry["blob_sha"] or _create_blob(conn_id, owner, name, entry["content"])
        tree_entries.append({
            "path": path, "mode": entry["mode"], "type": "blob", "sha": blob_sha,
        })
        if remote_sha:
            updated += 1
        else:
            created += 1
    if not tree_entries:
        return {
            "branch": branch,
            "commit_sha": parent or "",
            "tree_sha": base_tree or "",
            "files_created": 0,
            "files_updated": 0,
            "files_unchanged": unchanged,
            "total_files": len(entries),
            "committed": False,
            "ref_created": False,
            "ref_updated": False,
        }
    summary = _publish_commit(conn_id, owner, name, branch, parent, base_tree,
                              tree_entries, message)
    summary.update({
        "files_created": created,
        "files_updated": updated,
        "files_unchanged": unchanged,
        "total_files": len(entries),
        "committed": True,
    })
    return summary


__all__ = [
    "GithubRestError",
    "test_connection", "get_user_info", "list_repos", "get_repo_info",
    "create_repo", "read_file", "read_file_meta", "upload_file",
    "update_file", "delete_file", "list_files", "get_ref", "get_commit",
    "get_tree", "batch_commit", "batch_upsert",
]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
