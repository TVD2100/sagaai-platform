# -*- coding: utf-8 -*-
"""
Tests for core.github_tools_rest - orchestrator tools for the REST connector.

The underlying connector functions are mocked so the tests never touch the
network. Argument validation, error wrapping and metadata export are covered.
"""
import json
from unittest import mock

import pytest

import core.github_tools_rest as gtr


@mock.patch("core.github_connector_rest.list_repos")
def test_ghr_list_repos_ok(mock_list):
    mock_list.return_value = [{"full_name": "alice/repo1", "name": "repo1"}]
    result = gtr.ghr_list_repos(connector_id="abc", sort="created")
    assert result["ok"] is True
    assert result["result"][0]["name"] == "repo1"
    mock_list.assert_called_once_with("abc", sort="created")


def test_ghr_list_repos_missing_connector_id():
    result = gtr.ghr_list_repos()
    assert result["ok"] is False
    assert "connector_id" in result["error"]


@mock.patch("core.github_connector_rest.list_repos")
def test_ghr_list_repos_default_sort(mock_list):
    mock_list.return_value = []
    gtr.ghr_list_repos(connector_id="abc")
    mock_list.assert_called_once_with("abc", sort="updated")


@mock.patch("core.github_connector_rest.create_repo")
def test_ghr_create_repo_ok(mock_create):
    mock_create.return_value = {"full_name": "alice/newrepo", "name": "newrepo"}
    result = gtr.ghr_create_repo(
        connector_id="abc", name="newrepo", description="d", private=True,
        auto_init=False,
    )
    assert result["ok"] is True
    mock_create.assert_called_once_with(
        "abc", "newrepo", description="d", private=True, auto_init=False
    )


def test_ghr_create_repo_missing_name():
    result = gtr.ghr_create_repo(connector_id="abc")
    assert result["ok"] is False
    assert "name" in result["error"]


@mock.patch("core.github_connector_rest.read_file")
def test_ghr_read_file_ok(mock_read):
    mock_read.return_value = {"path": "a.txt", "content": "Hi", "sha": "s"}
    result = gtr.ghr_read_file(
        connector_id="abc", repo="alice/r", path="a.txt", branch="dev"
    )
    assert result["ok"] is True
    mock_read.assert_called_once_with("abc", "alice/r", "a.txt", branch="dev")


@pytest.mark.parametrize("kwargs", [
    {"connector_id": "abc", "path": "a.txt"},
    {"connector_id": "abc", "repo": "alice/r"},
])
def test_ghr_read_file_missing_repo_or_path(kwargs):
    result = gtr.ghr_read_file(**kwargs)
    assert result["ok"] is False


@mock.patch("core.github_connector_rest.upload_file")
def test_ghr_upload_file_ok(mock_upload):
    mock_upload.return_value = {"path": "a.txt", "sha": "f1", "committed": True}
    result = gtr.ghr_upload_file(
        connector_id="abc", repo="alice/r", path="a.txt", content="hello",
        message="m", branch="main",
    )
    assert result["ok"] is True
    mock_upload.assert_called_once_with(
        "abc", "alice/r", "a.txt", "hello", message="m", branch="main"
    )


@mock.patch("core.github_connector_rest.update_file")
def test_ghr_update_file_ok(mock_update):
    mock_update.return_value = {"path": "a.txt", "sha": "f2", "committed": True}
    result = gtr.ghr_update_file(
        connector_id="abc", repo="alice/r", path="a.txt", content="v2",
        branch="dev", sha="x1",
    )
    assert result["ok"] is True
    mock_update.assert_called_once_with(
        "abc", "alice/r", "a.txt", "v2", message="", branch="dev", sha="x1"
    )


@mock.patch("core.github_connector_rest.delete_file")
def test_ghr_delete_file_ok(mock_delete):
    mock_delete.return_value = {"path": "a.txt", "committed": True}
    result = gtr.ghr_delete_file(
        connector_id="abc", repo="alice/r", path="a.txt",
        message="rm", branch="dev", sha="d1",
    )
    assert result["ok"] is True
    mock_delete.assert_called_once_with(
        "abc", "alice/r", "a.txt", message="rm", branch="dev", sha="d1"
    )


@mock.patch("core.github_connector_rest.list_files")
def test_ghr_list_files_ok(mock_list):
    mock_list.return_value = [{"name": "src", "path": "src", "type": "dir"}]
    result = gtr.ghr_list_files(
        connector_id="abc", repo="alice/r", path="docs", branch="main"
    )
    assert result["ok"] is True
    mock_list.assert_called_once_with("abc", "alice/r", path="docs", branch="main")


@mock.patch("core.github_connector_rest.batch_commit")
def test_ghr_batch_commit_with_list(mock_batch):
    mock_batch.return_value = {"commit_sha": "c1", "total_files": 2}
    files = [{"path": "a.txt", "content": "1"}, {"path": "b.txt", "content": "2"}]
    result = gtr.ghr_batch_commit(
        connector_id="abc", repo="alice/r", files=files,
        message="Release", branch="main",
    )
    assert result["ok"] is True
    mock_batch.assert_called_once_with(
        "abc", "alice/r", files, message="Release", branch="main"
    )


@mock.patch("core.github_connector_rest.batch_commit")
def test_ghr_batch_commit_with_json_string(mock_batch):
    mock_batch.return_value = {"commit_sha": "c2", "total_files": 1}
    files_json = json.dumps([{"path": "a.txt", "content": "1"}])
    result = gtr.ghr_batch_commit(
        connector_id="abc", repo="alice/r", files=files_json
    )
    assert result["ok"] is True
    mock_batch.assert_called_once_with(
        "abc", "alice/r", [{"path": "a.txt", "content": "1"}],
        message="", branch="",
    )


def test_ghr_batch_commit_invalid_json():
    result = gtr.ghr_batch_commit(
        connector_id="abc", repo="alice/r", files="{not-json"
    )
    assert result["ok"] is False
    assert "JSON" in result["error"]


def test_ghr_batch_commit_missing_files():
    result = gtr.ghr_batch_commit(connector_id="abc", repo="alice/r")
    assert result["ok"] is False
    assert "files" in result["error"]


@mock.patch("core.github_connector_rest.batch_commit", side_effect=ValueError("boom"))
def test_ghr_batch_commit_rejects_non_list_entry(mock_batch):
    gtr.ghr_batch_commit(connector_id="abc", repo="alice/r", files="[]")
    # Empty JSON list is rejected before the connector is called.
    mock_batch.assert_not_called()


@mock.patch("core.github_connector_rest.batch_upsert")
def test_ghr_batch_upsert_ok(mock_upsert):
    mock_upsert.return_value = {"committed": False, "files_unchanged": 2}
    files = [{"path": "a.txt", "content": "1"}]
    result = gtr.ghr_batch_upsert(
        connector_id="abc", repo="alice/r", files=files, branch="main"
    )
    assert result["ok"] is True
    mock_upsert.assert_called_once_with(
        "abc", "alice/r", files, message="", branch="main"
    )


@mock.patch("core.github_connector_rest.read_file")
def test_ghr_tool_wraps_connector_error(mock_read):
    from core.github_connector_rest import GithubRestError
    mock_read.side_effect = GithubRestError("auth failed", status=401)
    result = gtr.ghr_read_file(connector_id="abc", repo="r", path="x")
    assert result["ok"] is False
    assert result["error"] == "auth failed"


@mock.patch("core.github_connector_rest.read_file")
def test_ghr_tool_wraps_unexpected_error(mock_read):
    mock_read.side_effect = RuntimeError("kaboom")
    result = gtr.ghr_read_file(connector_id="abc", repo="r", path="x")
    assert result["ok"] is False
    assert "failed" in result["error"]


def test_get_tools_metadata():
    tools = gtr.get_tools()
    names = {t["name"] for t in tools}
    assert names == {
        "ghr_list_repos",
        "ghr_create_repo",
        "ghr_read_file",
        "ghr_upload_file",
        "ghr_update_file",
        "ghr_delete_file",
        "ghr_list_files",
        "ghr_test_connection",
        "ghr_get_repo_info",
        "ghr_read_file_meta",
        "ghr_get_ref",
        "ghr_get_commit",
        "ghr_get_tree",
        "ghr_batch_commit",
        "ghr_batch_upsert",
    }
    for tool in tools:
        assert tool["desc"]
        assert tool["name"] in gtr.TOOLS


@mock.patch("core.github_connector_rest.test_connection")
def test_ghr_test_connection_ok(mock_test):
    mock_test.return_value = {"ok": True, "login": "alice", "name": "Alice"}
    result = gtr.ghr_test_connection(connector_id="abc")
    assert result["ok"] is True
    assert result["result"]["login"] == "alice"
    mock_test.assert_called_once_with("abc")


@mock.patch("core.github_connector_rest.get_repo_info")
def test_ghr_get_repo_info_ok(mock_info):
    mock_info.return_value = {"full_name": "alice/r", "default_branch": "main"}
    result = gtr.ghr_get_repo_info(connector_id="abc", repo="alice/r")
    assert result["ok"] is True
    assert result["result"]["full_name"] == "alice/r"
    mock_info.assert_called_once_with("abc", "alice/r")


def test_ghr_get_repo_info_missing_repo():
    result = gtr.ghr_get_repo_info(connector_id="abc")
    assert result["ok"] is False
    assert "repo" in result["error"]


@mock.patch("core.github_connector_rest.read_file_meta")
def test_ghr_read_file_meta_ok(mock_meta):
    mock_meta.return_value = {"path": "a.txt", "sha": "s1", "size": 2}
    result = gtr.ghr_read_file_meta(
        connector_id="abc", repo="alice/r", path="a.txt", branch="dev",
    )
    assert result["ok"] is True
    assert result["result"]["sha"] == "s1"
    mock_meta.assert_called_once_with("abc", "alice/r", "a.txt", branch="dev")


def test_ghr_read_file_meta_missing_path():
    result = gtr.ghr_read_file_meta(connector_id="abc", repo="alice/r")
    assert result["ok"] is False
    assert "path" in result["error"]


@mock.patch("core.github_connector_rest.get_ref")
def test_ghr_get_ref_ok(mock_ref):
    mock_ref.return_value = {"ref": "refs/heads/main", "sha": "c1"}
    result = gtr.ghr_get_ref(connector_id="abc", repo="alice/r", branch="main")
    assert result["ok"] is True
    assert result["result"]["sha"] == "c1"
    mock_ref.assert_called_once_with(
        "abc", "alice/r", branch="main", resolve=True,
    )


@mock.patch("core.github_connector_rest.get_commit")
def test_ghr_get_commit_ok(mock_commit):
    mock_commit.return_value = {"sha": "c1", "message": "m"}
    result = gtr.ghr_get_commit(
        connector_id="abc", repo="alice/r", commit_sha="c1",
    )
    assert result["ok"] is True
    mock_commit.assert_called_once_with("abc", "alice/r", "c1")


def test_ghr_get_commit_missing_sha():
    result = gtr.ghr_get_commit(connector_id="abc", repo="alice/r")
    assert result["ok"] is False
    assert "commit_sha" in result["error"]


@mock.patch("core.github_connector_rest.get_tree")
def test_ghr_get_tree_ok(mock_tree):
    mock_tree.return_value = {"tree_sha": "t1", "entries": []}
    result = gtr.ghr_get_tree(
        connector_id="abc", repo="alice/r", branch="main", recursive=True,
    )
    assert result["ok"] is True
    mock_tree.assert_called_once_with(
        "abc", "alice/r", branch="main", recursive=True,
    )