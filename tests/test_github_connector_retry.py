# -*- coding: utf-8 -*-
"""
Tests for the GET-only transient-network retry in core.github_connector_rest.

The HTTP layer (``_session``) is mocked, so the suite never touches the
network, and ``time.sleep`` is mocked too, so the backoff delays are asserted
without real waiting. Policy under test: only read-only GET requests are
retried, and only on requests.Timeout / requests.ConnectionError (2 retries,
3 attempts max, backoff 0.5/1.5 s); write methods and HTTP error responses
are never retried.
"""
from contextlib import contextmanager
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
        "github_rest", "GitHub-REST-Retry-Test", "ghr-retry-token"
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
    """Scripted requests.Session stand-in recording every request call."""

    def __init__(self, responses):
        self.calls = []
        self.responses = list(responses)

    def request(self, method, url, json=None, params=None, headers=None,
                timeout=None):
        self.calls.append({"method": method, "url": url})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@contextmanager
def _patched_request(session):
    """Patch _session and time.sleep; yield the sleep mock for assertions."""
    with mock.patch.object(ghr, "_session", return_value=session), \
            mock.patch.object(ghr.time, "sleep") as sleep_mock:
        yield sleep_mock


def _sleeps(sleep_mock):
    return [call.args[0] for call in sleep_mock.call_args_list]


def test_get_retries_timeout_then_succeeds(isolated_connector):
    session = FakeSession([
        requests.Timeout("read timed out"),
        requests.Timeout("read timed out"),
        FakeResponse(200, {"login": "alice"}),
    ])
    with _patched_request(session) as sleep_mock:
        result = ghr._request(isolated_connector, "GET", "/user")
    assert result == {"login": "alice"}
    assert len(session.calls) == 3
    assert session.calls[0]["method"] == "GET"
    assert _sleeps(sleep_mock) == [0.5, 1.5]


def test_get_retries_connection_error_then_succeeds(isolated_connector):
    session = FakeSession([
        requests.ConnectionError("connection reset"),
        FakeResponse(200, {"ok": True}),
    ])
    with _patched_request(session) as sleep_mock:
        result = ghr._request(isolated_connector, "GET", "/user")
    assert result == {"ok": True}
    assert len(session.calls) == 2
    assert _sleeps(sleep_mock) == [0.5]


def test_get_retry_exhausted_raises_and_does_not_leak_token(isolated_connector):
    session = FakeSession([
        requests.Timeout("t1"), requests.Timeout("t2"), requests.Timeout("t3"),
    ])
    with _patched_request(session) as sleep_mock:
        with pytest.raises(ghr.GithubRestError) as exc_info:
            ghr._request(isolated_connector, "GET", "/user")
    assert "network error" in str(exc_info.value)
    assert "ghr-retry-token" not in str(exc_info.value)
    assert len(session.calls) == 3
    assert _sleeps(sleep_mock) == [0.5, 1.5]


def test_post_timeout_is_not_retried(isolated_connector):
    session = FakeSession([requests.Timeout("read timed out")])
    with _patched_request(session) as sleep_mock:
        with pytest.raises(ghr.GithubRestError):
            ghr._request(isolated_connector, "POST", "/user/repos",
                         body={"name": "x"})
    assert len(session.calls) == 1
    assert session.calls[0]["method"] == "POST"
    assert _sleeps(sleep_mock) == []


def test_http_500_is_not_retried(isolated_connector):
    session = FakeSession([FakeResponse(500, {"message": "boom"})])
    with _patched_request(session) as sleep_mock:
        with pytest.raises(ghr.GithubRestError) as exc_info:
            ghr._request(isolated_connector, "GET", "/user")
    assert exc_info.value.status == 500
    assert "boom" in str(exc_info.value)
    assert len(session.calls) == 1
    assert _sleeps(sleep_mock) == []


def test_http_404_is_not_retried(isolated_connector):
    session = FakeSession([FakeResponse(404, {})])
    with _patched_request(session) as sleep_mock:
        with pytest.raises(ghr.GithubRestError) as exc_info:
            ghr._request(isolated_connector, "GET", "/user")
    assert exc_info.value.status == 404
    assert len(session.calls) == 1
    assert _sleeps(sleep_mock) == []


def test_other_network_errors_are_not_retried(isolated_connector):
    session = FakeSession([RuntimeError("weird socket state")])
    with _patched_request(session) as sleep_mock:
        with pytest.raises(ghr.GithubRestError) as exc_info:
            ghr._request(isolated_connector, "GET", "/user")
    assert "network error" in str(exc_info.value)
    assert len(session.calls) == 1
    assert _sleeps(sleep_mock) == []
