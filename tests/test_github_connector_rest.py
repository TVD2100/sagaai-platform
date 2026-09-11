# -*- coding: utf-8 -*-
"""
Tests for core.github_connector_rest - the direct GitHub REST API v3 connector.

The HTTP layer (``_request``) is mocked in most tests so the suite never
touches the network. A few tests exercise ``_request`` itself through a fake
``requests.Session`` to verify bearer authentication, 204 handling and error
mapping. Batch-pipeline tests verify the optimized flow:
blobs -> ONE tree -> ONE commit -> ONE ref update for the whole batch.
"""
import base64
from unittest import mock

import pytest

import core.paths
from core import connectors
from core import github_connector_rest as ghr


@pytest.fixture()
def isolated_connector(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp dir and create a github_rest connection."""
    monkeypatch.setattr(core.paths, "DATA_DIR", str(tmp_path))
    created = connectors.create_connection(
        "github_rest", "GitHub-REST-Test", "ghr-foo-token"
    )
    return created["id"]


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeSession:
    """Records requests.Session.request calls and returns scripted responses."""

    def __init__(self):
        self.calls = []
        self.responses = []

    def request(self, method, url, json=None, params=None, headers=None,
                timeout=None):
        self.calls.append({
            "method": method,
            "url": url,
            "json": json,
            "params": params,
            "headers": headers,
            "timeout": timeout,
        })
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeAPI:
    """Records low-level _request calls and dispatches them to a handler."""

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def __call__(self, conn_id, method, url_path, body=None, params=None,
                 timeout=ghr._DEFAULT_TIMEOUT):
        self.calls.append({
            "conn_id": conn_id,
            "method": method,
            "url": url_path,
            "body": body,
            "params": params,
            "timeout": timeout,
        })
        if self.handler is None:
            raise AssertionError(f"Unexpected request: {method} {url_path}")
        result = self.handler(self.calls[-1])
        if isinstance(result, Exception):
            raise result
        return result


# ---------------------------------------------------------------------------
# Error mapping / quoting helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status,data,expected", [
    (401, {"message": "Bad credentials"}, "authentication failed"),
    (403, {}, "forbidden"),
    (403, {"message": "rate limit"}, "rate limit"),
    (404, {}, "not found"),
    (404, {"message": "No such repo"}, "No such repo"),
    (422, {"message": "Validation failed"}, "Validation failed"),
    (500, {}, "GitHub API error (500)"),
])
def test_describe_rest_error(status, data, expected):
    message = ghr._describe_rest_error(status, data)
    assert expected.lower() in message.lower()


@pytest.mark.parametrize("raw,expected", [
    ("README.md", "README.md"),
    ("docs/a b.txt", "docs/a%20b.txt"),
    ("img/картинка.png", "img/%D0%BA%D0%B0%D1%80%D1%82%D0%B8%D0%BD%D0%BA%D0%B0.png"),
])
def test_quote_path(raw, expected):
    assert ghr._quote_path(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("main", "main"),
    ("feature/x", "feature/x"),
    ("feature x", "feature%20x"),
])
def test_quote_branch(raw, expected):
    assert ghr._quote_branch(raw) == expected


def test_git_blob_sha_known_values():
    assert ghr._git_blob_sha("") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert ghr._git_blob_sha("hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


# ---------------------------------------------------------------------------
# Low-level request layer
# ---------------------------------------------------------------------------


def test_session_sets_github_api_headers():
    session = ghr._session("https://api.github.com")
    assert session.headers["Accept"] == "application/vnd.github+json"
    assert session.headers["X-GitHub-Api-Version"] == ghr.API_VERSION_HEADER


def test_api_base_default_and_override(isolated_connector):
    assert ghr._api_base(isolated_connector) == "https://api.github.com"
    with mock.patch.object(
        connectors, "get_connection_full",
        return_value={"api_base": "https://ghe.example.com/api/v3/"},
    ):
        assert ghr._api_base(isolated_connector) == "https://ghe.example.com/api/v3"


def test_request_uses_bearer_token_and_returns_json(isolated_connector):
    session = FakeSession()
    session.responses = [FakeResponse(200, {"login": "alice"})]
    with mock.patch.object(ghr, "_session", return_value=session):
        result = ghr._request(isolated_connector, "GET", "/user")
    assert result == {"login": "alice"}
    call = session.calls[0]
    assert call["headers"]["Authorization"] == "Bearer ghr-foo-token"
    assert call["url"] == "https://api.github.com/user"
    assert call["method"] == "GET"
    assert call["timeout"] == ghr._DEFAULT_TIMEOUT


def test_request_204_returns_none(isolated_connector):
    session = FakeSession()
    session.responses = [FakeResponse(204)]
    with mock.patch.object(ghr, "_session", return_value=session):
        assert ghr._request(isolated_connector, "DELETE", "/x") is None


def test_request_http_error_maps_to_github_rest_error(isolated_connector):
    session = FakeSession()
    session.responses = [FakeResponse(401, {"message": "Bad credentials"})]
    with mock.patch.object(ghr, "_session", return_value=session):
        with pytest.raises(ghr.GithubRestError) as exc_info:
            ghr._request(isolated_connector, "GET", "/user")
    assert exc_info.value.status == 401
    assert "authentication failed" in str(exc_info.value)


def test_request_network_error_wrapped_and_token_not_leaked(isolated_connector):
    session = FakeSession()
    session.responses = [RuntimeError("connection reset")]
    with mock.patch.object(ghr, "_session", return_value=session):
        with pytest.raises(ghr.GithubRestError) as exc_info:
            ghr._request(isolated_connector, "GET", "/user")
    assert "network error" in str(exc_info.value)
    assert "ghr-foo-token" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Basic API functions
# ---------------------------------------------------------------------------


def test_repo_spec_owner_form(isolated_connector):
    assert ghr._repo_spec(isolated_connector, "alice/repo1") == ("alice", "repo1")


def test_repo_spec_bare_name_uses_login(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value={"login": "alice"}):
        owner, name = ghr._repo_spec(isolated_connector, "repo1")
    assert (owner, name) == ("alice", "repo1")


@pytest.mark.parametrize("bad", ["", "   ", "/", "alice/", "/repo"])
def test_repo_spec_bad_values(isolated_connector, bad):
    with pytest.raises(ghr.GithubRestError):
        ghr._repo_spec(isolated_connector, bad)


def test_get_user_info(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value={
        "login": "alice", "name": "Alice", "email": "a@example.com",
        "public_repos": 7, "html_url": "https://github.com/alice",
    }):
        info = ghr.get_user_info(isolated_connector)
    assert info == {
        "login": "alice", "name": "Alice", "email": "a@example.com",
        "public_repos": 7, "html_url": "https://github.com/alice",
    }


def test_test_connection_updates_account(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value={
        "login": "TVD2100", "name": "Test User", "id": 123,
        "html_url": "https://github.com/TVD2100",
    }) as req:
        result = ghr.test_connection(isolated_connector)
    req.assert_called_once_with(isolated_connector, "GET", "/user")
    assert result["ok"] is True
    assert result["login"] == "TVD2100"
    assert result["name"] == "Test User"
    assert connectors.get_connection(isolated_connector)["account"] == "TVD2100"


def test_list_repos_paginates_until_short_page(isolated_connector):
    page1 = [{"full_name": f"alice/r{i}"} for i in range(3)]
    page2 = [{"full_name": "alice/r3"}]
    with mock.patch.object(ghr, "_LIST_PER_PAGE", 3), mock.patch.object(
        ghr, "_request", side_effect=[page1, page2]
    ) as req:
        repos = ghr.list_repos(isolated_connector, sort="created")
    assert [r["full_name"] for r in repos] == [
        "alice/r0", "alice/r1", "alice/r2", "alice/r3"
    ]
    assert req.call_args_list[0].kwargs["params"]["page"] == 1
    assert req.call_args_list[0].kwargs["params"]["sort"] == "created"
    assert req.call_args_list[1].kwargs["params"]["page"] == 2


def test_get_repo_info(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value={
        "full_name": "alice/repo1", "name": "repo1",
        "owner": {"login": "alice"}, "private": True,
        "description": "Desc", "html_url": "https://github.com/alice/repo1",
        "default_branch": "master",
    }) as req:
        info = ghr.get_repo_info(isolated_connector, "alice/repo1")
    req.assert_called_once_with(isolated_connector, "GET", "/repos/alice/repo1")
    assert info["private"] is True
    assert info["default_branch"] == "master"


def test_create_repo_posts_and_maps_fields(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value={
        "full_name": "alice/newrepo", "name": "newrepo",
        "html_url": "https://github.com/alice/newrepo",
        "default_branch": "main", "private": True,
    }) as req:
        result = ghr.create_repo(
            isolated_connector, "newrepo", description="t", private=True,
            auto_init=False,
        )
    call = req.call_args
    assert call[0][1:3] == ("POST", "/user/repos")
    assert call[1]["body"] == {
        "name": "newrepo", "description": "t", "private": True, "auto_init": False,
    }
    assert result["full_name"] == "alice/newrepo"


def test_create_repo_empty_name_rejected(isolated_connector):
    with pytest.raises(ghr.GithubRestError):
        ghr.create_repo(isolated_connector, " ")


# ---------------------------------------------------------------------------
# File CRUD via the Contents API
# ---------------------------------------------------------------------------


def test_read_file_meta(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value={
        "path": "a.txt", "sha": "sha1", "size": 12,
        "html_url": "https://github.com/alice/r/blob/dev/a.txt",
    }) as req:
        meta = ghr.read_file_meta(isolated_connector, "alice/r", "a.txt", branch="dev")
    call = req.call_args
    assert call[0][1:3] == ("GET", "/repos/alice/r/contents/a.txt")
    assert call[1]["params"] == {"ref": "dev"}
    assert meta["sha"] == "sha1"


def test_read_file_meta_empty_path_rejected(isolated_connector):
    with pytest.raises(ghr.GithubRestError):
        ghr.read_file_meta(isolated_connector, "alice/r", " ")


def test_read_file_decodes_base64(isolated_connector):
    encoded = base64.b64encode("Привет, мир!".encode("utf-8")).decode("ascii")
    with mock.patch.object(ghr, "_request", return_value={
        "path": "hello.txt", "content": encoded, "encoding": "base64",
        "sha": "sha1", "html_url": "https://example/",
    }) as req:
        result = ghr.read_file(
            isolated_connector, "alice/r", "hello.txt", branch="dev"
        )
    assert req.call_args[1]["params"] == {"ref": "dev"}
    assert result["content"] == "Привет, мир!"
    assert result["sha"] == "sha1"


def test_read_file_quotes_path(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value={}) as req:
        ghr.read_file(isolated_connector, "alice/r", "docs/a b.txt")
    assert req.call_args[0][2] == "/repos/alice/r/contents/docs/a%20b.txt"


def test_upload_file_builds_put_payload(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value={
        "content": {"sha": "f1"}, "commit": {"sha": "c1"},
    }) as req:
        result = ghr.upload_file(
            isolated_connector, "alice/r", "main.py", "print(1)",
            message="Add main.py", branch="main",
        )
    call = req.call_args
    assert call[0][1:3] == ("PUT", "/repos/alice/r/contents/main.py")
    body = call[1]["body"]
    assert body["message"] == "Add main.py"
    assert body["branch"] == "main"
    assert base64.b64decode(body["content"]).decode("utf-8") == "print(1)"
    assert result == {
        "path": "main.py", "sha": "f1", "committed": True, "commit_sha": "c1",
    }


def test_upload_file_conflict_422(isolated_connector):
    with mock.patch.object(
        ghr, "_request", side_effect=ghr.GithubRestError("v", status=422)
    ):
        with pytest.raises(ghr.GithubRestError) as exc_info:
            ghr.upload_file(isolated_connector, "alice/r", "a.txt", "x")
    assert "already exists" in str(exc_info.value)


def test_upload_file_server_error_wrapped(isolated_connector):
    with mock.patch.object(
        ghr, "_request", side_effect=ghr.GithubRestError("boom", status=500)
    ):
        with pytest.raises(ghr.GithubRestError) as exc_info:
            ghr.upload_file(isolated_connector, "alice/r", "a.txt", "x")
    assert "Cannot upload file" in str(exc_info.value)


def test_update_file_uses_passed_sha(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value={
        "content": {"sha": "f2"}, "commit": {"sha": "c2"},
    }) as req:
        result = ghr.update_file(
            isolated_connector, "alice/r", "a.txt", "v2", message="m",
            branch="dev", sha="xyz-1",
        )
    body = req.call_args[1]["body"]
    assert body["sha"] == "xyz-1"
    assert body["branch"] == "dev"
    assert base64.b64decode(body["content"]).decode("utf-8") == "v2"
    assert result["sha"] == "f2"
    assert result["commit_sha"] == "c2"


def test_update_file_fetches_sha_when_missing(isolated_connector):
    def handler(call):
        if call["method"] == "GET":
            return {"sha": "fetched-sha"}
        return {"content": {"sha": "f3"}, "commit": {"sha": "c3"}}

    api = FakeAPI(handler)
    with mock.patch.object(ghr, "_request", new=api):
        result = ghr.update_file(isolated_connector, "alice/r", "a.txt", "v3")
    assert [c["method"] for c in api.calls] == ["GET", "PUT"]
    assert api.calls[1]["body"]["sha"] == "fetched-sha"
    assert result["sha"] == "f3"


def test_delete_file_returns_clean_summary(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value=None) as req:
        result = ghr.delete_file(
            isolated_connector, "alice/r", "old.py", message="rm",
            branch="dev", sha="d1",
        )
    call = req.call_args
    assert call[0][1:3] == ("DELETE", "/repos/alice/r/contents/old.py")
    assert call[1]["body"] == {"message": "rm", "sha": "d1", "branch": "dev"}
    assert result == {"path": "old.py", "committed": True}


def test_list_files_dir_and_file(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value=[
        {"name": "src", "path": "src", "type": "dir"},
        {"name": "README.md", "path": "README.md", "type": "file"},
    ]) as req:
        files = ghr.list_files(isolated_connector, "alice/r", branch="main")
    assert req.call_args[1]["params"] == {"ref": "main"}
    assert files == [
        {"name": "src", "path": "src", "type": "dir"},
        {"name": "README.md", "path": "README.md", "type": "file"},
    ]


def test_list_files_single_item_wrapped_in_list(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value={
        "name": "a.txt", "path": "a.txt", "type": "file",
    }):
        files = ghr.list_files(isolated_connector, "alice/r")
    assert files == [{"name": "a.txt", "path": "a.txt", "type": "file"}]


def test_list_files_error_wrapped(isolated_connector):
    with mock.patch.object(
        ghr, "_request", side_effect=ghr.GithubRestError("x", status=404)
    ):
        with pytest.raises(ghr.GithubRestError) as exc_info:
            ghr.list_files(isolated_connector, "alice/r")
    assert "Cannot list directory" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Git Data API helpers
# ---------------------------------------------------------------------------


def test_get_ref_resolves_branch_head(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value={
        "ref": "refs/heads/main", "object": {"sha": "abc123", "type": "commit"},
    }) as req:
        ref = ghr.get_ref(isolated_connector, "alice/r", branch="main")
    assert req.call_args[0][2] == "/repos/alice/r/git/ref/heads/main"
    assert ref == {
        "ref": "refs/heads/main", "sha": "abc123", "object_type": "commit",
    }


def test_get_ref_branch_with_slash(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value={}) as req:
        ghr.get_ref(isolated_connector, "alice/r", branch="feature/x")
    assert req.call_args[0][2] == "/repos/alice/r/git/ref/heads/feature/x"


def test_get_ref_empty_branch_uses_default(isolated_connector):
    with mock.patch.object(ghr, "_default_branch", return_value="main"), mock.patch.object(
        ghr, "_request",
        return_value={"ref": "refs/heads/main", "object": {"sha": "x", "type": "commit"}},
    ) as req:
        ref = ghr.get_ref(isolated_connector, "alice/r")
    assert req.call_args[0][2] == "/repos/alice/r/git/ref/heads/main"
    assert ref["sha"] == "x"


def test_get_commit_maps_fields(isolated_connector):
    with mock.patch.object(ghr, "_request", return_value={
        "sha": "c1", "message": "init", "tree": {"sha": "t1"},
        "parents": [{"sha": "p1"}, {"sha": "p2"}],
    }) as req:
        commit = ghr.get_commit(isolated_connector, "alice/r", "c1")
    assert req.call_args[0][2] == "/repos/alice/r/git/commits/c1"
    assert commit == {
        "sha": "c1", "message": "init", "tree_sha": "t1", "parents": ["p1", "p2"],
    }


def test_get_commit_empty_sha_rejected(isolated_connector):
    with pytest.raises(ghr.GithubRestError):
        ghr.get_commit(isolated_connector, "alice/r", " ")


def test_get_tree_recursive_entries(isolated_connector):
    def handler(call):
        if call["url"] == "/repos/alice/r/git/ref/heads/main":
            return {"ref": "refs/heads/main", "object": {"sha": "c1", "type": "commit"}}
        if call["url"] == "/repos/alice/r/git/commits/c1":
            return {"sha": "c1", "tree": {"sha": "t1"}, "parents": []}
        if call["url"] == "/repos/alice/r/git/trees/t1":
            return {"sha": "t1", "truncated": False, "tree": [
                {"path": "a.txt", "type": "blob", "sha": "b1", "mode": "100644", "size": 3},
                {"path": "src", "type": "tree", "sha": "d1", "mode": "040000"},
            ]}
        raise AssertionError(call["url"])

    api = FakeAPI(handler)
    with mock.patch.object(ghr, "_request", new=api):
        tree = ghr.get_tree(isolated_connector, "alice/r", branch="main", recursive=True)
    assert tree["tree_sha"] == "t1"
    assert tree["truncated"] is False
    assert tree["entries"][0] == {
        "path": "a.txt", "type": "blob", "sha": "b1", "mode": "100644", "size": 3,
    }
    assert "size" not in tree["entries"][1]
    assert api.calls[2]["params"] == {"recursive": "1"}


# ---------------------------------------------------------------------------
# Batch input normalization
# ---------------------------------------------------------------------------


def test_normalize_batch_files_ok():
    files = [
        {"path": "a.txt", "content": "hello"},
        {"path": "b.txt", "blob_sha": "x1"},
    ]
    result = ghr._normalize_batch_files(files)
    assert result[0]["content"] == "hello"
    assert result[0]["mode"] == "100644"
    assert result[1]["blob_sha"] == "x1"


def test_normalize_batch_files_rejects_duplicates():
    files = [{"path": "a.txt", "content": "1"}, {"path": "a.txt", "content": "2"}]
    with pytest.raises(ghr.GithubRestError):
        ghr._normalize_batch_files(files)


def test_normalize_batch_files_rejects_missing_content_and_blob():
    with pytest.raises(ghr.GithubRestError):
        ghr._normalize_batch_files([{"path": "a.txt"}])


@pytest.mark.parametrize("bad", [
    "x", None, [], [1], [{"content": "x"}],
])
def test_normalize_batch_files_rejects_bad_input(bad):
    with pytest.raises(ghr.GithubRestError):
        ghr._normalize_batch_files(bad)


def test_normalize_batch_files_strips_leading_slash():
    result = ghr._normalize_batch_files([{"path": "/a.txt", "content": "1"}])
    assert result[0]["path"] == "a.txt"


# ---------------------------------------------------------------------------
# Optimized batch publishing (batch_commit)
# ---------------------------------------------------------------------------


def test_batch_commit_creates_branch_in_empty_repo(isolated_connector):
    """On an empty repo GitHub answers 404 for the branch ref; the pipeline
    must fall back to POST /git/refs to create the branch."""

    def handler(call):
        method, url = call["method"], call["url"]
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            raise ghr.GithubRestError("not found", status=404)
        if method == "POST" and url.endswith("/git/blobs"):
            return {"sha": "blob-" + call["body"]["content"]}
        if method == "POST" and url.endswith("/git/trees"):
            return {"sha": "tree-1"}
        if method == "POST" and url.endswith("/git/commits"):
            return {"sha": "commit-1"}
        if method == "POST" and url.endswith("/git/refs"):
            return {"ref": "refs/heads/main",
                    "object": {"sha": "commit-1", "type": "commit"}}
        raise AssertionError(f"Unexpected: {method} {url}")

    api = FakeAPI(handler)
    files = [
        {"path": "a.txt", "content": "AAA"},
        {"path": "b.txt", "content": "BBB"},
    ]
    with mock.patch.object(ghr, "_request", new=api):
        result = ghr.batch_commit(
            isolated_connector, "alice/repo", files, branch="main"
        )
    assert result["commit_sha"] == "commit-1"
    assert result["tree_sha"] == "tree-1"
    assert result["ref_created"] is True
    assert result["ref_updated"] is False
    assert result["files_created"] == 2
    assert result["total_files"] == 2
    assert [c["url"] for c in api.calls] == [
        "/repos/alice/repo/git/ref/heads/main",
        "/repos/alice/repo/git/blobs",
        "/repos/alice/repo/git/blobs",
        "/repos/alice/repo/git/trees",
        "/repos/alice/repo/git/commits",
        "/repos/alice/repo/git/refs",
    ]
    tree_entries = api.calls[3]["body"]["tree"]
    assert {e["path"] for e in tree_entries} == {"a.txt", "b.txt"}
    assert api.calls[5]["body"] == {"ref": "refs/heads/main", "sha": "commit-1"}


def test_batch_commit_empty_repo_409_treated_as_no_branch(isolated_connector):
    """Real GitHub answers 409 'Git Repository is empty' for an empty repo's
    ref instead of 404; the pipeline must still fall back to POST /git/refs."""

    def handler(call):
        method, url = call["method"], call["url"]
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            raise ghr.GithubRestError("Git Repository is empty.", status=409)
        if method == "POST" and url.endswith("/git/blobs"):
            return {"sha": "blob-" + call["body"]["content"]}
        if method == "POST" and url.endswith("/git/trees"):
            return {"sha": "tree-1"}
        if method == "POST" and url.endswith("/git/commits"):
            return {"sha": "commit-1"}
        if method == "POST" and url.endswith("/git/refs"):
            return {"ref": "refs/heads/main",
                    "object": {"sha": "commit-1", "type": "commit"}}
        raise AssertionError(f"Unexpected: {method} {url}")

    api = FakeAPI(handler)
    files = [{"path": "a.txt", "content": "AAA"}]
    with mock.patch.object(ghr, "_request", new=api):
        result = ghr.batch_commit(
            isolated_connector, "alice/repo", files, branch="main"
        )
    assert result["commit_sha"] == "commit-1"
    assert result["ref_created"] is True
    assert result["ref_updated"] is False
    assert [c["url"] for c in api.calls] == [
        "/repos/alice/repo/git/ref/heads/main",
        "/repos/alice/repo/git/blobs",
        "/repos/alice/repo/git/trees",
        "/repos/alice/repo/git/commits",
        "/repos/alice/repo/git/refs",
    ]


def test_batch_commit_unrelated_409_still_raises(isolated_connector):
    """A 409 that is NOT the empty-repo signal must propagate."""

    def handler(call):
        method, url = call["method"], call["url"]
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            raise ghr.GithubRestError("Conflict", status=409)
        raise AssertionError(f"Unexpected: {method} {url}")

    api = FakeAPI(handler)
    files = [{"path": "a.txt", "content": "AAA"}]
    with mock.patch.object(ghr, "_request", new=api):
        with pytest.raises(ghr.GithubRestError) as exc:
            ghr.batch_commit(
                isolated_connector, "alice/repo", files, branch="main"
            )
    assert exc.value.status == 409


def test_batch_commit_empty_repo_blob_409_gives_helpful_error(isolated_connector):
    """On a completely empty repo GitHub also rejects blob creation with
    409 'Git Repository is empty'; the user must get a helpful error."""

    def handler(call):
        method, url = call["method"], call["url"]
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            raise ghr.GithubRestError("not found", status=404)
        if method == "POST" and url.endswith("/git/blobs"):
            raise ghr.GithubRestError("Git Repository is empty.", status=409)
        raise AssertionError(f"Unexpected: {method} {url}")

    api = FakeAPI(handler)
    files = [{"path": "a.txt", "content": "AAA"}]
    with mock.patch.object(ghr, "_request", new=api):
        with pytest.raises(ghr.GithubRestError) as exc:
            ghr.batch_commit(
                isolated_connector, "alice/repo", files, branch="main"
            )
    assert exc.value.status == 409
    assert "first commit" in str(exc.value)


def test_batch_commit_updates_existing_branch_with_base_tree(isolated_connector):
    def handler(call):
        method, url = call["method"], call["url"]
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return {"ref": "refs/heads/main",
                    "object": {"sha": "parent-1", "type": "commit"}}
        if method == "GET" and url.endswith("/git/commits/parent-1"):
            return {"sha": "parent-1", "tree": {"sha": "base-tree"}, "parents": []}
        if method == "POST" and url.endswith("/git/blobs"):
            return {"sha": "blob-" + call["body"]["content"]}
        if method == "POST" and url.endswith("/git/trees"):
            return {"sha": "tree-2"}
        if method == "POST" and url.endswith("/git/commits"):
            return {"sha": "commit-2"}
        if method == "PATCH" and url.endswith("/git/refs/heads/main"):
            return {"ref": "refs/heads/main",
                    "object": {"sha": "commit-2", "type": "commit"}}
        raise AssertionError(f"Unexpected: {method} {url}")

    api = FakeAPI(handler)
    files = [{"path": "c.txt", "content": "CCC"}]
    with mock.patch.object(ghr, "_request", new=api):
        result = ghr.batch_commit(
            isolated_connector, "alice/repo", files,
            message="Release v1", branch="main",
        )
    assert result["commit_sha"] == "commit-2"
    assert result["ref_updated"] is True
    assert result["ref_created"] is False
    tree_call = api.calls[3]
    assert tree_call["body"] == {
        "tree": [{"path": "c.txt", "mode": "100644", "type": "blob", "sha": "blob-CCC"}],
        "base_tree": "base-tree",
    }
    commit_call = api.calls[4]
    assert commit_call["body"]["parents"] == ["parent-1"]
    assert commit_call["body"]["message"] == "Release v1"
    patch_call = api.calls[5]
    assert patch_call["body"] == {"sha": "commit-2", "force": False}


def test_batch_commit_uses_provided_blob_sha_without_creating_blob(isolated_connector):
    def handler(call):
        method, url = call["method"], call["url"]
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            raise ghr.GithubRestError("missing", status=404)
        if method == "POST" and url.endswith("/git/trees"):
            return {"sha": "tree-3"}
        if method == "POST" and url.endswith("/git/commits"):
            return {"sha": "commit-3"}
        if method == "POST" and url.endswith("/git/refs"):
            return {"ref": "refs/heads/main",
                    "object": {"sha": "commit-3", "type": "commit"}}
        raise AssertionError(f"Unexpected: {method} {url}")

    api = FakeAPI(handler)
    files = [{"path": "a.txt", "blob_sha": "premade-blob"}]
    with mock.patch.object(ghr, "_request", new=api):
        result = ghr.batch_commit(
            isolated_connector, "alice/repo", files, branch="main"
        )
    assert result["commit_sha"] == "commit-3"
    assert not any(c["url"].endswith("/git/blobs") for c in api.calls)
    tree_entries = api.calls[1]["body"]["tree"]
    assert tree_entries[0]["sha"] == "premade-blob"


# ---------------------------------------------------------------------------
# Optimized batch upsert (content diffing)
# ---------------------------------------------------------------------------


def test_batch_upsert_skips_unchanged_files(isolated_connector):
    local_sha_a = ghr._git_blob_sha("AAA")
    local_sha_b = ghr._git_blob_sha("BBB")

    def handler(call):
        method, url = call["method"], call["url"]
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return {"ref": "refs/heads/main",
                    "object": {"sha": "parent-1", "type": "commit"}}
        if method == "GET" and url.endswith("/git/commits/parent-1"):
            return {"sha": "parent-1", "tree": {"sha": "base-tree"}, "parents": []}
        if method == "GET" and url.endswith("/git/trees/base-tree"):
            return {"truncated": False, "tree": [
                {"path": "a.txt", "type": "blob", "sha": local_sha_a},
                {"path": "b.txt", "type": "blob", "sha": local_sha_b},
            ]}
        raise AssertionError(f"Unexpected: {method} {url}")

    api = FakeAPI(handler)
    files = [
        {"path": "a.txt", "content": "AAA"},
        {"path": "b.txt", "content": "BBB"},
    ]
    with mock.patch.object(ghr, "_request", new=api):
        result = ghr.batch_upsert(
            isolated_connector, "alice/repo", files, branch="main"
        )
    assert result["committed"] is False
    assert result["files_unchanged"] == 2
    assert result["files_created"] == 0
    assert result["files_updated"] == 0
    assert not any(c["url"].endswith("/git/blobs") for c in api.calls)
    assert not any(
        c["method"] == "POST" and "/git/trees" in c["url"] for c in api.calls
    )


def test_batch_upsert_creates_blobs_only_for_changed_files(isolated_connector):
    def handler(call):
        method, url = call["method"], call["url"]
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return {"ref": "refs/heads/main",
                    "object": {"sha": "parent-1", "type": "commit"}}
        if method == "GET" and url.endswith("/git/commits/parent-1"):
            return {"sha": "parent-1", "tree": {"sha": "base-tree"}, "parents": []}
        if method == "GET" and url.endswith("/git/trees/base-tree"):
            return {"truncated": False, "tree": [
                {"path": "a.txt", "type": "blob", "sha": ghr._git_blob_sha("OLD")},
            ]}
        if method == "POST" and url.endswith("/git/blobs"):
            return {"sha": "blob-" + call["body"]["content"]}
        if method == "POST" and url.endswith("/git/trees"):
            return {"sha": "tree-4"}
        if method == "POST" and url.endswith("/git/commits"):
            return {"sha": "commit-4"}
        if method == "PATCH" and url.endswith("/git/refs/heads/main"):
            return {"ref": "refs/heads/main",
                    "object": {"sha": "commit-4", "type": "commit"}}
        raise AssertionError(f"Unexpected: {method} {url}")

    api = FakeAPI(handler)
    files = [
        {"path": "a.txt", "content": "NEW"},
        {"path": "b.txt", "content": "BBB"},
    ]
    with mock.patch.object(ghr, "_request", new=api):
        result = ghr.batch_upsert(
            isolated_connector, "alice/repo", files, branch="main"
        )
    assert result["committed"] is True
    assert result["files_updated"] == 1
    assert result["files_created"] == 1
    assert result["files_unchanged"] == 0
    blob_urls = [
        c["url"] for c in api.calls
        if c["method"] == "POST" and c["url"].endswith("/git/blobs")
    ]
    assert blob_urls == [
        "/repos/alice/repo/git/blobs",
        "/repos/alice/repo/git/blobs",
    ]
    tree_call = next(
        c for c in api.calls
        if c["method"] == "POST" and c["url"].endswith("/git/trees")
    )
    assert {e["path"] for e in tree_call["body"]["tree"]} == {"a.txt", "b.txt"}
    assert tree_call["body"]["base_tree"] == "base-tree"


def test_batch_upsert_skips_blob_sha_entries_matching_remote(isolated_connector):
    def handler(call):
        method, url = call["method"], call["url"]
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return {"ref": "refs/heads/main",
                    "object": {"sha": "parent-1", "type": "commit"}}
        if method == "GET" and url.endswith("/git/commits/parent-1"):
            return {"sha": "parent-1", "tree": {"sha": "base-tree"}, "parents": []}
        if method == "GET" and url.endswith("/git/trees/base-tree"):
            return {"truncated": False, "tree": [
                {"path": "a.txt", "type": "blob", "sha": "remote-blob"},
            ]}
        raise AssertionError(f"Unexpected: {method} {url}")

    api = FakeAPI(handler)
    files = [{"path": "a.txt", "blob_sha": "remote-blob"}]
    with mock.patch.object(ghr, "_request", new=api):
        result = ghr.batch_upsert(
            isolated_connector, "alice/repo", files, branch="main"
        )
    assert result["committed"] is False
    assert result["files_unchanged"] == 1


def test_batch_commit_tree_chunked_over_limit(isolated_connector):
    """Trees with more than _TREE_CREATE_CHUNK entries are split into
    several tree-creation calls."""

    def handler(call):
        method, url = call["method"], call["url"]
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            raise ghr.GithubRestError("missing", status=404)
        if method == "POST" and url.endswith("/git/blobs"):
            return {"sha": "blob-" + call["body"]["content"]}
        if method == "POST" and url.endswith("/git/trees"):
            n_entries = len(call["body"]["tree"])
            return {"sha": f"tree-{n_entries}"}
        if method == "POST" and url.endswith("/git/commits"):
            return {"sha": "commit-5"}
        if method == "POST" and url.endswith("/git/refs"):
            return {"ref": "refs/heads/main",
                    "object": {"sha": "commit-5", "type": "commit"}}
        raise AssertionError(f"Unexpected: {method} {url}")

    api = FakeAPI(handler)
    with mock.patch.object(ghr, "_TREE_CREATE_CHUNK", 5):
        files = [{"path": f"f{i}", "content": "x"} for i in range(8)]
        with mock.patch.object(ghr, "_request", new=api):
            result = ghr.batch_commit(
                isolated_connector, "alice/repo2", files, branch="main"
            )
    assert result["committed"] is True
    tree_urls = [
        c["url"] for c in api.calls
        if c["method"] == "POST" and c["url"].endswith("/git/trees")
    ]
    assert len(tree_urls) == 2
    # The second tree call chains onto the first created tree.
    assert api.calls[-3]["body"]["base_tree"] == "tree-5"


def test_batch_commit_retries_ref_update_after_fast_forward(isolated_connector):
    """A 422 'Update is not a fast forward' on the first ref PATCH must be
    recovered once: re-read the head, rebuild tree+commit on the new parent,
    and retry the ref update."""
    patch_calls = {"count": 0}

    def handler(call):
        method, url = call["method"], call["url"]
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return {"ref": "refs/heads/main",
                    "object": {"sha": "parent-1", "type": "commit"}}
        if method == "POST" and url.endswith("/git/blobs"):
            return {"sha": "blob-1"}
        if method == "POST" and url.endswith("/git/trees"):
            return {"sha": "tree-1"}
        if method == "POST" and url.endswith("/git/commits"):
            return {"sha": "commit-1"}
        if method == "PATCH" and url.endswith("/git/refs/heads/main"):
            patch_calls["count"] += 1
            if patch_calls["count"] == 1:
                raise ghr.GithubRestError(
                    "Update is not a fast forward", status=422)
            return {"ref": "refs/heads/main",
                    "object": {"sha": "commit-2", "type": "commit"}}
        raise AssertionError(f"Unexpected: {method} {url}")

    # The recovery path re-reads the branch head and the new parent commit.
    api = FakeAPI(handler)
    recovered_head = {
        "ref": "refs/heads/main",
        "object": {"sha": "parent-2", "type": "commit"},
    }
    recovered_commit = {
        "sha": "parent-2",
        "tree": {"sha": "base-tree-2"},
        "parents": [],
    }

    def _request(conn_id, method, url_path, body=None, params=None,
                 timeout=ghr._DEFAULT_TIMEOUT):
        if (method == "GET" and
                url_path.endswith("/git/ref/heads/main")):
            # Initial head read vs. recovery head read are hard to tell
            # apart inside FakeAPI; drive that via a call counter.
            api.calls.append({"conn_id": conn_id, "method": method,
                              "url": url_path, "body": body,
                              "params": params, "timeout": timeout})
            if sum(1 for c in api.calls
                   if c["method"] == "GET" and
                   c["url"].endswith("/git/ref/heads/main")) == 1:
                return {"ref": "refs/heads/main",
                        "object": {"sha": "parent-1", "type": "commit"}}
            return recovered_head
        if method == "GET" and url_path.endswith("/git/commits/parent-1"):
            return {"sha": "parent-1",
                    "tree": {"sha": "base-tree"}, "parents": []}
        if method == "GET" and url_path.endswith("/git/commits/parent-2"):
            return recovered_commit
        return api(conn_id, method, url_path, body=body, params=params,
                   timeout=timeout)

    files = [{"path": "c.txt", "content": "CCC"}]
    with mock.patch.object(ghr, "_request", new=_request):
        result = ghr.batch_commit(
            isolated_connector, "alice/repo", files, branch="main")

    assert result["ref_retried"] is True
    assert result["ref_updated"] is True
    assert patch_calls["count"] == 2


def test_batch_commit_ref_retry_raises_when_head_unresolvable(isolated_connector):
    """When the 422 recovery cannot resolve the new head, the error must be
    raised instead of retried forever."""
    def handler(call):
        method, url = call["method"], call["url"]
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return {"ref": "refs/heads/main",
                    "object": {"sha": "parent-1", "type": "commit"}}
        if method == "POST":
            return {"sha": "x"}
        if method == "PATCH" and url.endswith("/git/refs/heads/main"):
            raise ghr.GithubRestError(
                "Update is not a fast forward", status=422)
        raise AssertionError(f"Unexpected: {method} {url}")

    api = FakeAPI(handler)

    def _request(conn_id, method, url_path, body=None, params=None,
                 timeout=ghr._DEFAULT_TIMEOUT):
        if method == "GET" and url_path.endswith("/git/ref/heads/main"):
            api.calls.append({"conn_id": conn_id, "method": method,
                              "url": url_path, "body": body,
                              "params": params, "timeout": timeout})
            if sum(1 for c in api.calls
                   if c["method"] == "GET" and
                   c["url"].endswith("/git/ref/heads/main")) == 1:
                return {"ref": "refs/heads/main",
                        "object": {"sha": "parent-1", "type": "commit"}}
            return {"ref": "refs/heads/main", "object": {}}
        if method == "GET" and url_path.endswith("/git/commits/parent-1"):
            return {"sha": "parent-1",
                    "tree": {"sha": "base-tree"}, "parents": []}
        return api(conn_id, method, url_path, body=body, params=params,
                   timeout=timeout)

    files = [{"path": "c.txt", "content": "CCC"}]
    with mock.patch.object(ghr, "_request", new=_request):
        with pytest.raises(ghr.GithubRestError) as exc:
            ghr.batch_commit(
                isolated_connector, "alice/repo", files, branch="main")
    assert "cannot be resolved" in str(exc.value)
