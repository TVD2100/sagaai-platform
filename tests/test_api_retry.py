# -*- coding: utf-8 -*-
"""Unit tests for the transparent connection-retry layer (Steps 1-2).

Covers the building blocks added to core.api_layer and
core.assistant_tools:
  * retry_call: retry-then-succeed, exhaustion with the ``attempts`` marker,
    non-retryable passthrough, callback reporting, callback crash safety;
  * _retry_params: env overrides, invalid-value fallback, clamping;
  * send_request: transparent retries across the full bearer path.

All tests force SAGAAI_NETWORK_RETRY_DELAY=0 so no real sleep happens.
"""

import pytest

from core.api_errors import NetworkError, ProviderHTTPError, RequestTimeoutError


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """Keep retries instant across the whole module."""
    monkeypatch.setenv("SAGAAI_NETWORK_RETRY_DELAY", "0")
    monkeypatch.setenv("SAGAAI_NETWORK_RETRY_ATTEMPTS", "3")


# --- retry_call ------------------------------------------------------------

def test_retry_call_succeeds_after_transport_failures():
    """A function failing twice with NetworkError succeeds on attempt 3."""
    from core.api_layer import retry_call

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise NetworkError("connection reset", service="TestSvc")
        return "recovered"

    assert retry_call(flaky) == "recovered"
    assert calls["n"] == 3


def test_retry_call_retries_timeout_errors():
    """RequestTimeoutError is a retryable transport error as well."""
    from core.api_layer import retry_call

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RequestTimeoutError(service="TestSvc")
        return "ok"

    assert retry_call(flaky) == "ok"
    assert calls["n"] == 2


def test_retry_call_exhausted_marks_attempts():
    """After the final attempt the LAST exception keeps an attempts marker."""
    from core.api_layer import retry_call

    def always_fails():
        raise NetworkError("no route", service="TestSvc")

    with pytest.raises(NetworkError) as exc_info:
        retry_call(always_fails)
    assert exc_info.value.attempts == 3


def test_retry_call_passes_through_non_retryable_errors():
    """ProviderHttpError (4xx/5xx) is NOT retried - exactly one call."""
    from core.api_layer import retry_call

    calls = {"n": 0}

    def http_error():
        calls["n"] += 1
        raise ProviderHTTPError(500, "boom", service="TestSvc")

    with pytest.raises(ProviderHTTPError):
        retry_call(http_error)
    assert calls["n"] == 1


def test_retry_call_reports_via_callback():
    """retry_callback receives attempt/attempts/delay/error before each retry."""
    from core.api_layer import retry_call

    events = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RequestTimeoutError(service="TestSvc")
        return "done"

    assert retry_call(flaky, retry_callback=events.append) == "done"
    assert [e["attempt"] for e in events] == [1, 2]
    assert all(e["attempts"] == 3 for e in events)
    assert all(e["delay"] == 0 for e in events)
    assert all("error" in e for e in events)


def test_retry_call_callback_crash_does_not_break_retries():
    """A crashing UI callback must never stop the retry flow."""
    from core.api_layer import retry_call

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise NetworkError("down", service="TestSvc")
        return "ok"

    def bad_callback(_event):
        raise RuntimeError("ui crashed")

    assert retry_call(flaky, retry_callback=bad_callback) == "ok"
    assert calls["n"] == 2


# --- _retry_params ---------------------------------------------------------

def test_retry_params_defaults_without_env(monkeypatch):
    from core.api_layer import _retry_params

    monkeypatch.delenv("SAGAAI_NETWORK_RETRY_DELAY", raising=False)
    monkeypatch.delenv("SAGAAI_NETWORK_RETRY_ATTEMPTS", raising=False)
    assert _retry_params() == (30.0, 3)


def test_retry_params_env_overrides(monkeypatch):
    from core.api_layer import _retry_params

    monkeypatch.setenv("SAGAAI_NETWORK_RETRY_DELAY", "1.5")
    monkeypatch.setenv("SAGAAI_NETWORK_RETRY_ATTEMPTS", "5")
    assert _retry_params() == (1.5, 5)


def test_retry_params_invalid_env_falls_back_to_defaults(monkeypatch):
    from core.api_layer import _retry_params

    monkeypatch.setenv("SAGAAI_NETWORK_RETRY_DELAY", "abc")
    monkeypatch.setenv("SAGAAI_NETWORK_RETRY_ATTEMPTS", "xyz")
    assert _retry_params() == (30.0, 3)


def test_retry_params_clamps_negative_values(monkeypatch):
    from core.api_layer import _retry_params

    monkeypatch.setenv("SAGAAI_NETWORK_RETRY_DELAY", "-5")
    monkeypatch.setenv("SAGAAI_NETWORK_RETRY_ATTEMPTS", "0")
    assert _retry_params() == (0.0, 1)


def test_retry_call_budget_is_reread_each_attempt(monkeypatch):
    """The retry budget comes fresh from env on every iteration."""
    from core.api_layer import retry_call

    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise NetworkError("down", service="TestSvc")

    monkeypatch.setenv("SAGAAI_NETWORK_RETRY_ATTEMPTS", "1")
    with pytest.raises(NetworkError) as exc_info:
        retry_call(always_fails)
    assert calls["n"] == 1
    assert exc_info.value.attempts == 1


# --- send_request integration ---------------------------------------------

_SVC = {
    "DeepSeek": {
        "auth_type": "bearer",
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "config_key": "deepseek_key",
        "temp_default": 0.7,
    }
}

_CFG = {
    "deepseek_key": "sk-test-key",
    "yandex_iam_token": "iam-token",
    "yandex_cloud_id": "folder-id",
    "gigachat_creds": "creds-base64",
}

_SKILL = {
    "service": "DeepSeek",
    "model": "deepseek-chat",
    "temperature": 0.3,
    "text": "System prompt.",
    "id": "abc123",
}


def test_send_request_retries_transport_errors_then_succeeds():
    """ConnectionError -> Timeout -> success is one transparent call."""
    import requests as real_requests
    from unittest.mock import MagicMock, patch

    from core.api_layer import send_request

    good_resp = MagicMock()
    good_resp.status_code = 200
    good_resp.json.return_value = {"choices": [{"message": {"content": "После ретраев"}}]}

    with patch("core.api_layer.get_services", return_value=_SVC), \
         patch("core.api_layer.load_config", return_value=_CFG), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post", side_effect=[
             real_requests.exceptions.ConnectionError("сеть отвалилась"),
             real_requests.exceptions.Timeout("таймаут"),
             good_resp,
         ]) as mock_post:
        result = send_request("Вопрос", dict(_SKILL))

    assert result == "После ретраев"
    assert mock_post.call_count == 3


def test_send_request_exhausted_raises_with_attempts():
    """Permanent outage re-raises NetworkError after 3 attempts."""
    import requests as real_requests
    from unittest.mock import patch

    from core.api_layer import send_request

    with patch("core.api_layer.get_services", return_value=_SVC), \
         patch("core.api_layer.load_config", return_value=_CFG), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post",
               side_effect=real_requests.exceptions.ConnectionError("no net")) as mock_post:
        with pytest.raises(NetworkError) as exc_info:
            send_request("Вопрос", dict(_SKILL))

    assert mock_post.call_count == 3
    assert exc_info.value.attempts == 3


def test_send_request_provider_http_error_is_not_retried():
    """ProviderHTTPError (500) fails fast after a single HTTP call."""
    from unittest.mock import MagicMock, patch

    from core.api_layer import send_request

    bad_resp = MagicMock()
    bad_resp.status_code = 500
    bad_resp.json.return_value = {"error": {"message": "Internal error"}}

    with patch("core.api_layer.get_services", return_value=_SVC), \
         patch("core.api_layer.load_config", return_value=_CFG), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post", return_value=bad_resp) as mock_post:
        with pytest.raises(ProviderHTTPError):
            send_request("Вопрос", dict(_SKILL))

    assert mock_post.call_count == 1
