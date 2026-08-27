# -*- coding: utf-8 -*-
"""
Tests for core.github_connector with mocked PyGithub objects.
"""
from unittest import mock

import pytest

import core.paths
from core import connectors
from core import github_connector as gh


@pytest.fixture()
def isolated_connector(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp dir and create a github connection."""
    monkeypatch.setattr(core.paths, "DATA_DIR", str(tmp_path))
    created = connectors.create_connection("github", "GitHub-Test", "ghp-foo-token")
    return created["id"]


@mock.patch("core.github_connector._ensure_github")
def test_test_connection_sets_account(mock_ensure, isolated_connector):
    import github
    mock_ensure.return_value = github

    class FakeUser:
        login = "TVD2100"
        name = "Test User"
        id = 123
        html_url = "https://github.com/TVD2100"

    class FakeGithub:
        def __init__(self, token):
            self.token = token

        def get_user(self):
            return FakeUser()

    with mock.patch.object(github, "Github", FakeGithub):
        result = gh.test_connection(isolated_connector)
    assert result["ok"] is True
    assert result["login"] == "TVD2100"
    assert result["name"] == "Test User"
    assert result["html_url"] == "https://github.com/TVD2100"
    conn = connectors.get_connection(isolated_connector)
    assert conn["account"] == "TVD2100"


@mock.patch("core.github_connector._ensure_github")
def test_test_connection_auth_error(mock_ensure, isolated_connector):
    import github
    mock_ensure.return_value = github

    class AuthGithub:
        def __init__(self, token):
            pass

        def get_user(self):
            raise github.GithubException(401, {"message": "Bad credentials"})

    with mock.patch.object(github, "Github", AuthGithub):
        with pytest.raises(gh.GithubConnectorError) as exc_info:
            gh.test_connection(isolated_connector)
    assert "authentication failed" in str(exc_info.value)


@mock.patch("core.github_connector._ensure_github")
def test_get_user_info(mock_ensure, isolated_connector):
    import github
    mock_ensure.return_value = github

    class FakeUser:
        login = "alice"
        name = "Alice"
        email = "a@example.com"
        public_repos = 7
        html_url = "https://github.com/alice"

    class FakeGithub:
        def __init__(self, token):
            pass

        def get_user(self):
            return FakeUser()

    with mock.patch.object(github, "Github", FakeGithub):
        info = gh.get_user_info(isolated_connector)
    assert info["login"] == "alice"
    assert info["public_repos"] == 7


@mock.patch("core.github_connector._ensure_github")
def test_list_repos(mock_ensure, isolated_connector):
    import github
    mock_ensure.return_value = github

    class Repo:
        full_name = "alice/repo1"
        name = "repo1"
        private = False
        description = "First"
        html_url = "https://github.com/alice/repo1"
        default_branch = "main"

    class FakeUser:
        login = "alice"

        def get_repos(self, sort=None):
            return [Repo()]

    class FakeGithub:
        def __init__(self, token):
            pass

        def get_user(self):
            return FakeUser()

    with mock.patch.object(github, "Github", FakeGithub):
        repos = gh.list_repos(isolated_connector)
    assert len(repos) == 1
    assert repos[0]["full_name"] == "alice/repo1"


@mock.patch("core.github_connector._ensure_github")
def test_get_repo_info_owner_repo(mock_ensure, isolated_connector):
    import github
    mock_ensure.return_value = github

    class Repo:
        full_name = "alice/repo1"
        name = "repo1"
        private = True
        description = "Desc"
        html_url = "https://github.com/alice/repo1"
        default_branch = "master"

    class FakeGithub:
        def __init__(self, token):
            pass

        def get_repo(self, spec):
            assert spec == "alice/repo1"
            return Repo()

    with mock.patch.object(github, "Github", FakeGithub):
        info = gh.get_repo_info(isolated_connector, "alice/repo1")
    assert info["name"] == "repo1"
    assert info["owner"] == "alice"
    assert info["private"] is True


@mock.patch("core.github_connector._ensure_github")
def test_get_repo_info_bare_name_uses_login(mock_ensure, isolated_connector):
    import github
    mock_ensure.return_value = github

    class User:
        login = "alice"

    class FakeGithub:
        def __init__(self, token):
            pass

        def get_user(self):
            return User()

        def get_repo(self, spec):
            assert spec == "alice/myrepo"
            return mock.Mock()

    with mock.patch.object(github, "Github", FakeGithub):
        gh.get_repo_info(isolated_connector, "myrepo")


@mock.patch("core.github_connector._ensure_github")
def test_create_repo(mock_ensure, isolated_connector):
    import github
    mock_ensure.return_value = github

    class NewRepo:
        full_name = "alice/newrepo"
        name = "newrepo"
        html_url = "https://github.com/alice/newrepo"
        default_branch = "main"

    class User:
        login = "alice"

        def create_repo(self, **kw):
            assert kw["name"] == "newrepo"
            assert kw["description"] == "test"
            return NewRepo()

    class FakeGithub:
        def __init__(self, token):
            pass

        def get_user(self):
            return User()

    with mock.patch.object(github, "Github", FakeGithub):
        result = gh.create_repo(isolated_connector, "newrepo", description="test")
    assert result["full_name"] == "alice/newrepo"


@mock.patch("core.github_connector._ensure_github")
def test_read_file_decodes_base64(mock_ensure, isolated_connector):
    import base64
    import github
    mock_ensure.return_value = github

    class Content:
        path = "README.md"
        content = base64.b64encode(b"Hello world").decode()
        sha = "abc123"
        html_url = "https://github.com/alice/repo/blob/main/README.md"

    class Repo:
        def get_contents(self, path, **kw):
            assert path == "README.md"
            return Content()

    class User:
        login = "alice"

    class FakeGithub:
        def __init__(self, token):
            pass

        def get_user(self):
            return User()

        def get_repo(self, spec):
            return Repo()

    with mock.patch.object(github, "Github", FakeGithub):
        result = gh.read_file(isolated_connector, "alice/repo", "README.md")
    assert result["content"] == "Hello world"
    assert result["sha"] == "abc123"


@mock.patch("core.github_connector._ensure_github")
def test_upload_file_calls_create_file(mock_ensure, isolated_connector):
    import github
    mock_ensure.return_value = github

    class Repo:
        def create_file(self, **kw):
            assert kw["path"] == "main.py"
            assert kw["content"] == "print(1)"
            assert kw["message"] == "Add main.py"
            return {"commit": {"sha": "c1"}, "content": {"sha": "f1"}}

    class User:
        login = "alice"

    class FakeGithub:
        def __init__(self, token):
            pass

        def get_user(self):
            return User()

        def get_repo(self, spec):
            return Repo()

    with mock.patch.object(github, "Github", FakeGithub):
        result = gh.upload_file(isolated_connector, "alice/repo", "main.py", "print(1)")
    assert result["committed"] is True
    assert result["sha"] == "f1"
    assert result["commit_sha"] == "c1"


@mock.patch("core.github_connector._ensure_github")
def test_upload_file_conflict(mock_ensure, isolated_connector):
    import github
    mock_ensure.return_value = github

    class Repo:
        def create_file(self, **kw):
            raise github.GithubException(
                422, {"message": "Invalid request", "errors": [{"message": "file already exists"}]}, headers={}
            )

    class User:
        login = "alice"

    class FakeGithub:
        def __init__(self, token):
            pass

        def get_user(self):
            return User()

        def get_repo(self, spec):
            return Repo()

    with mock.patch.object(github, "Github", FakeGithub):
        with pytest.raises(gh.GithubConnectorError) as exc_info:
            gh.upload_file(isolated_connector, "alice/repo", "main.py", "x")
    assert "already exists" in str(exc_info.value)


@mock.patch("core.github_connector._ensure_github")
def test_update_file_uses_passed_sha(mock_ensure, isolated_connector):
    import github
    mock_ensure.return_value = github

    class Repo:
        def update_file(self, **kw):
            assert kw["sha"] == "xyz-1"
            return {"commit": {"sha": "c2"}, "content": {"sha": "f2"}}

    class User:
        login = "alice"

    class FakeGithub:
        def __init__(self, token):
            pass

        def get_user(self):
            return User()

        def get_repo(self, spec):
            return Repo()

    with mock.patch.object(github, "Github", FakeGithub):
        result = gh.update_file(isolated_connector, "alice/repo", "main.py", "v2", sha="xyz-1")
    assert result["sha"] == "f2"


@mock.patch("core.github_connector._ensure_github")
def test_delete_file_uses_passed_sha(mock_ensure, isolated_connector):
    import github
    mock_ensure.return_value = github

    class Repo:
        def delete_file(self, **kw):
            assert kw["sha"] == "xyz-9"
            return {"commit": {"sha": "c3"}}

    class User:
        login = "alice"

    class FakeGithub:
        def __init__(self, token):
            pass

        def get_user(self):
            return User()

        def get_repo(self, spec):
            return Repo()

    with mock.patch.object(github, "Github", FakeGithub):
        result = gh.delete_file(isolated_connector, "alice/repo", "old.py", sha="xyz-9")
    assert result == {"path": "old.py", "committed": True}


@mock.patch("core.github_connector._ensure_github")
def test_list_files(mock_ensure, isolated_connector):
    import github
    mock_ensure.return_value = github

    class FakeContent:
        name = "src"
        path = "src"
        type = "dir"

    class Repo:
        def get_contents(self, path, **kw):
            return [FakeContent()]

    class User:
        login = "alice"

    class FakeGithub:
        def __init__(self, token):
            pass

        def get_user(self):
            return User()

        def get_repo(self, spec):
            return Repo()

    with mock.patch.object(github, "Github", FakeGithub):
        files = gh.list_files(isolated_connector, "alice/repo")
    assert files == [{"name": "src", "path": "src", "type": "dir"}]


@mock.patch("core.github_connector._ensure_github")
def test_repo_name_validation(mock_ensure, isolated_connector):
    import github
    mock_ensure.return_value = github

    with mock.patch.object(github, "Github", lambda token: None):
        with pytest.raises(gh.GithubConnectorError):
            gh.read_file(isolated_connector, "", "x")
        with pytest.raises(gh.GithubConnectorError):
            gh.read_file(isolated_connector, "alice/repo", "")
