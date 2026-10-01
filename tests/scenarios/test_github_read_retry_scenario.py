# -*- coding: utf-8 -*-
"""tests/scenarios/test_github_read_retry_scenario.py - scenario tests for the
GitHub connector retry policy through the PUBLIC API (self-reflection item 3).

Scenario 1 - the network hiccups: a public read (get_user_info) survives two
    transient errors (timeout, then connection reset) and returns data; the
    user never sees the blips.
Scenario 2 - GitHub is unreachable: after 3 attempts the public read fails
    with a clean GithubRestError that does not leak the token.
Scenario 3 - a write must NOT be silently repeated: create_repo hit by a
    read timeout fails immediately with exactly ONE POST attempt (a retry
    could create a duplicate repository).
"""
from __future__ import annotations

from unittest import mock

import pytest

requests = pytest.importorskip("requests")

import core.paths
from core import connectors
from core import github_connector_rest as ghr


@pytest.fixture()
def isolated_connector(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp dir and create a github_rest connection."""
    monkeypatch.setattr(core.paths, "DATA_DIR", str(tmp_path))
    created = connectors.create_connection(
        "github_rest", "GitHub-REST-Scenario", "ghr-scenario-token"
    )
    return created["id"]


class _FakeResponse:
    """Minimal requests.Response stand-in (200 JSON payload)."""

    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    """Scripted requests.Session stand-in recording request calls."""

    def __init__(self, script):
        self.calls = []
        self._script = list(script)

    def request(self, method, url, json=None, params=None, headers=None,
                timeout=None):
        self.calls.append({"method": method, "url": url})
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_public_read_survives_transient_network_errors(isolated_connector):
    # given: a flaky network - a timeout, then a reset, then success
    session = _FakeSession([
        requests.Timeout("read timed out"),
        requests.ConnectionError("connection reset by peer"),
        _FakeResponse({
            "login": "alice", "name": "Alice", "email": "a@example.com",
            "public_repos": 3, "html_url": "https://github.com/alice",
        }),
    ])

    # when: the user runs a public read (get_user_info)
    with mock.patch.object(ghr, "_session", return_value=session), \
            mock.patch.object(ghr.time, "sleep") as sleep_mock:
        info = ghr.get_user_info(isolated_connector)

    # then: the read succeeded after 3 attempts with the 0.5/1.5 s backoff
    assert info["login"] == "alice"
    assert [c["method"] for c in session.calls] == ["GET", "GET", "GET"]
    assert [c.args[0] for c in sleep_mock.call_args_list] == [0.5, 1.5]


def test_public_read_unreachable_fails_cleanly_without_token_leak(
        isolated_connector):
    # given: GitHub is unreachable (every attempt raises)
    session = _FakeSession([requests.ConnectionError("dns failure")] * 3)

    # when: the user runs a public read
    with mock.patch.object(ghr, "_session", return_value=session), \
            mock.patch.object(ghr.time, "sleep") as sleep_mock:
        with pytest.raises(ghr.GithubRestError) as exc_info:
            ghr.get_user_info(isolated_connector)

    # then: exactly 3 attempts, a clean error and no token in the message
    assert len(session.calls) == 3
    assert "network error" in str(exc_info.value)
    assert "ghr-scenario-token" not in str(exc_info.value)
    assert [c.args[0] for c in sleep_mock.call_args_list] == [0.5, 1.5]


def test_public_write_is_not_retried_on_timeout(isolated_connector):
    # given: a read timeout strikes a write request
    session = _FakeSession([requests.Timeout("read timed out")])

    # when: the user creates a repository (POST)
    with mock.patch.object(ghr, "_session", return_value=session), \
            mock.patch.object(ghr.time, "sleep") as sleep_mock:
        with pytest.raises(ghr.GithubRestError):
            ghr.create_repo(isolated_connector, "my-new-repo", private=True)

    # then: exactly ONE POST went out - a retry could duplicate the repo
    assert len(session.calls) == 1
    assert session.calls[0]["method"] == "POST"
    assert sleep_mock.call_args_list == []
