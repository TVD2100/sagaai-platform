# -*- coding: utf-8 -*-
"""
Tests for core.github_tools - orchestrator tool functions.

The underlying connector functions are mocked so the tests never touch the
network. The tools are verified for argument passing, error wrapping and
metadata export.
"""
from unittest import mock

import pytest

import core.github_tools as gt


@mock.patch("core.github_connector.list_repos")
def test_github_list_repos_ok(mock_list):
    mock_list.return_value = [{"full_name": "alice/repo1", "name": "repo1"}]
    result = gt.github_list_repos(connector_id="abc", sort="created")
    assert result["ok"] is True
    assert result["result"][0]["name"] == "repo1"
    mock_list.assert_called_once_with("abc", sort="created")


def test_github_list_repos_missing_connector_id():
    result = gt.github_list_repos()
    assert result["ok"] is False
    assert "connector_id" in result["error"]


@mock.patch("core.github_connector.list_repos")
def test_github_list_repos_default_sort(mock_list):
    mock_list.return_value = []
    gt.github_list_repos(connector_id="abc")
    mock_list.assert_called_once_with("abc", sort="updated")


@mock.patch("core.github_connector.create_repo")
def test_github_create_repo_ok(mock_create):
    mock_create.return_value = {"full_name": "alice/newrepo", "name": "newrepo"}
    result = gt.github_create_repo(
        connector_id="abc", name="newrepo", description="desc", private=False
    )
    assert result["ok"] is True
    mock_create.assert_called_once_with(
        "abc", "newrepo", description="desc", private=False
    )


def test_github_create_repo_missing_name():
    result = gt.github_create_repo(connector_id="abc")
    assert result["ok"] is False
    assert "name" in result["error"]


@mock.patch("core.github_connector.upload_file")
def test_github_upload_file_ok(mock_upload):
    mock_upload.return_value = {"path": "a.txt", "sha": "f1", "committed": True}
    result = gt.github_upload_file(
        connector_id="abc", repo="alice/r", path="a.txt", content="hello",
        message="m", branch="main",
    )
    assert result["ok"] is True
    mock_upload.assert_called_once_with(
        "abc", "alice/r", "a.txt", "hello", message="m", branch="main"
    )


@pytest.mark.parametrize("kwargs", [
    {"connector_id": "abc", "path": "a.txt", "content": "x"},
    {"connector_id": "abc", "repo": "alice/r", "content": "x"},
])
def test_github_upload_file_missing_repo_or_path(kwargs):
    result = gt.github_upload_file(**kwargs)
    assert result["ok"] is False


@mock.patch("core.github_connector.update_file")
def test_github_update_file_ok(mock_update):
    mock_update.return_value = {"path": "a.txt", "sha": "f2", "committed": True}
    result = gt.github_update_file(
        connector_id="abc", repo="alice/r", path="a.txt", content="v2", sha="x"
    )
    assert result["ok"] is True
    mock_update.assert_called_once_with(
        "abc", "alice/r", "a.txt", "v2", message="", branch="", sha="x"
    )


@mock.patch("core.github_connector.read_file")
def test_github_read_file_ok(mock_read):
    mock_read.return_value = {"path": "README.md", "content": "Hello", "sha": "s"}
    result = gt.github_read_file(
        connector_id="abc", repo="alice/r", path="README.md", branch="dev"
    )
    assert result["ok"] is True
    assert result["result"]["content"] == "Hello"
    mock_read.assert_called_once_with("abc", "alice/r", "README.md", branch="dev")


@mock.patch("core.github_connector.read_file")
def test_github_tool_wraps_connector_error(mock_read):
    from core.github_connector import GithubConnectorError
    mock_read.side_effect = GithubConnectorError("auth failed")
    result = gt.github_read_file(connector_id="abc", repo="r", path="x")
    assert result["ok"] is False
    assert result["error"] == "auth failed"


def test_get_tools_metadata():
    tools = gt.get_tools()
    names = {t["name"] for t in tools}
    assert names == {
        "github_list_repos",
        "github_create_repo",
        "github_upload_file",
        "github_update_file",
        "github_read_file",
    }
    for t in tools:
        assert t["desc"]
