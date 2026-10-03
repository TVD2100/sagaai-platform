# -*- coding: utf-8 -*-
"""tests/scenarios/test_yandex_responses_failures_scenario.py - user-level
scenario tests for Responses-API failure handling.

Scenarios (given -> when -> then):

  Scenario 1 - a legacy "xhigh" reasoning-effort value stays in the provider
               config (saved before the model matrix was corrected): for
               deepseek-v4.1-flash the value is dropped against the model
               catalog and the request goes out WITHOUT a reasoning block;
               deepseek-v4-flash keeps sending "xhigh".

  Scenario 2 - a stored empty assistant answer in the chat history does not
               break the next request: blank history items are filtered out
               of the Responses "input" payload (YandexAI and DeepSeek
               transports), preventing the HTTP 400 "Content of input is
               empty" cascade.

  Scenario 3 - HTTP 200 with status="failed" is surfaced as a real error:
               send_request raises ProviderResponseError carrying the
               provider message, and the Settings connection test returns
               (False, <provider text>) instead of a silent empty answer.

  Scenario 4 - the same failed body inside the assistant native
               function-call loop raises ProviderResponseError instead of
               silently ending with an empty answer.
"""
from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.api_errors import ProviderResponseError  # noqa: E402

FAILED_BODY = {
    "id": "resp-failed-1",
    "status": "failed",
    "error": {
        "code": "invalid_prompt",
        "message": "Error while calling model: 400: Invalid request",
    },
    "output": [],
    "usage": {"input_tokens": 0, "output_tokens": 0},
}

_FUNCTION_TOOL = {
    "type": "function",
    "name": "rag_search",
    "description": "Search a knowledge base",
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "query": {"type": "string"},
        },
    },
}


def _ok_body(text="ОК"):
    """A successful Responses body with one assistant message."""
    return {
        "status": "completed",
        "output": [
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": text}]},
        ],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }


def _mock_response(body, status_code=200):
    """A requests.Response stand-in with a JSON body."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    return resp


def _yandex_cfg(**overrides):
    """Persisted provider config for the YandexAI connection."""
    cfg = {"YANDEX_API_KEY": "iam-token", "YANDEX_FOLDER_ID": "folder-id"}
    cfg.update(overrides)
    return cfg


def _deepseek_service():
    """Minimal DeepSeek service definition (deepseek_responses transport)."""
    return {
        "name": "DeepSeek",
        "auth_type": "deepseek_responses",
        "base_url": "https://api.deepseek.com/v1",
        "config_key": "DEEPSEEK_API_KEY",
        "models": [{"id": "deepseek-v4.1-flash"}],
    }


def _effective_yandex_service():
    """The real YandexAI catalog as shipped in services/ + defaults/."""
    from core.services import discover_services
    return discover_services()["YandexAI"]


def _send_env(services, cfg):
    """Patch the catalog/config/context sources used by send_request."""
    stack = ExitStack()
    stack.enter_context(
        patch("core.api_layer.get_services", return_value=services))
    stack.enter_context(patch("core.api_layer.load_config", return_value=cfg))
    stack.enter_context(
        patch("core.api_layer.load_assistant_files_context", return_value=""))
    stack.enter_context(
        patch("core.api_layer._assistant_rag_context", return_value=""))
    return stack


def _yandex_assistant(model):
    """A minimal YandexAI assistant profile."""
    return {"service": "YandexAI", "model": model,
            "temperature": 0.3, "text": "sys"}


# --- Scenario 1 - legacy reasoning effort vs the model catalog -------------


def test_legacy_xhigh_config_dropped_for_v41_flash():
    """Given a legacy xhigh effort in the config, when deepseek-v4.1-flash
    (whose catalog entry has no xhigh) is asked, then the request carries no
    reasoning block and still succeeds."""
    from core.api_layer import send_request

    mock_resp = _mock_response(_ok_body("Ответ"))
    with _send_env({"YandexAI": _effective_yandex_service()},
                   _yandex_cfg(YandexAI_reasoning_effort="xhigh")), \
         patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        result = send_request("Привет", _yandex_assistant("deepseek-v4.1-flash"))

    assert result == "Ответ"
    payload = post.call_args[1]["json"]
    assert payload["model"] == "gpt://folder-id/deepseek-v4.1-flash"
    assert "reasoning" not in payload


def test_xhigh_still_sent_for_v4_flash():
    """Given the same legacy xhigh config, when deepseek-v4-flash (which
    supports xhigh) is asked, then reasoning.effort=xhigh is still sent."""
    from core.api_layer import send_request

    mock_resp = _mock_response(_ok_body("Ответ"))
    with _send_env({"YandexAI": _effective_yandex_service()},
                   _yandex_cfg(YandexAI_reasoning_effort="xhigh")), \
         patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        result = send_request("Привет", _yandex_assistant("deepseek-v4-flash"))

    assert result == "Ответ"
    payload = post.call_args[1]["json"]
    assert payload["reasoning"] == {"effort": "xhigh"}


# --- Scenario 2 - blank history items are filtered out ----------------------

_BLANK_HISTORY = [
    {"role": "user", "content": "вопрос"},
    {"role": "assistant", "content": ""},
    {"role": "user", "content": "   "},
    {"role": "assistant", "content": "ответ"},
]

_EXPECTED_INPUT = [
    {"role": "user", "content": "вопрос"},
    {"role": "assistant", "content": "ответ"},
    {"role": "user", "content": "продолжай"},
]


def test_blank_history_filtered_yandex():
    """Given a stored empty assistant answer, when the next YandexAI message
    is sent, then the input payload contains only non-blank items."""
    from core.api_layer import send_request

    mock_resp = _mock_response(_ok_body("ОК"))
    with _send_env({"YandexAI": _effective_yandex_service()}, _yandex_cfg()), \
         patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        result = send_request("продолжай",
                              _yandex_assistant("deepseek-v4.1-flash"),
                              history=_BLANK_HISTORY)

    assert result == "ОК"
    assert post.call_args[1]["json"]["input"] == _EXPECTED_INPUT


def test_blank_history_filtered_deepseek():
    """The same guarantee holds for the DeepSeek transport."""
    from core.api_layer import send_request

    assistant = {"service": "DeepSeek", "model": "deepseek-v4.1-flash",
                 "temperature": 0.3, "text": "sys"}
    mock_resp = _mock_response(_ok_body("ОК"))
    with _send_env({"DeepSeek": _deepseek_service()},
                   {"DEEPSEEK_API_KEY": "sk-test"}), \
         patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        result = send_request("продолжай", assistant, history=_BLANK_HISTORY)

    assert result == "ОК"
    assert post.call_args[1]["json"]["input"] == _EXPECTED_INPUT


# --- Scenario 3 - failed status is a real error -----------------------------


def test_failed_status_raises_with_provider_message_yandex():
    """Given HTTP 200 with status=failed, when send_request runs, then
    ProviderResponseError carries the provider code/message/id."""
    from core.api_layer import send_request

    mock_resp = _mock_response(FAILED_BODY)
    with _send_env({"YandexAI": _effective_yandex_service()}, _yandex_cfg()), \
         patch("core.api_layer.requests.post", return_value=mock_resp):
        with pytest.raises(ProviderResponseError) as excinfo:
            send_request("Привет", _yandex_assistant("deepseek-v4.1-flash"))

    err = excinfo.value
    assert err.status == "failed"
    assert err.provider_code == "invalid_prompt"
    assert "Error while calling model" in err.provider_message
    assert err.response_id == "resp-failed-1"
    assert err.service == "YandexAI"


def test_failed_status_raises_with_provider_message_deepseek():
    """The DeepSeek transport reports the same failed body as an error."""
    from core.api_layer import send_request

    assistant = {"service": "DeepSeek", "model": "deepseek-v4.1-flash",
                 "temperature": 0.3, "text": "sys"}
    mock_resp = _mock_response(FAILED_BODY)
    with _send_env({"DeepSeek": _deepseek_service()},
                   {"DEEPSEEK_API_KEY": "sk-test"}), \
         patch("core.api_layer.requests.post", return_value=mock_resp):
        with pytest.raises(ProviderResponseError) as excinfo:
            send_request("Привет", assistant)

    assert excinfo.value.provider_code == "invalid_prompt"
    assert excinfo.value.service == "DeepSeek"


def test_test_connection_reports_failed_body_yandex():
    """Given a failed body, when the connection test runs, then it returns
    (False, <provider text>) instead of OK."""
    from core.api_layer import test_connection

    svc = {"name": "YandexAI", "auth_type": "yandex_iam",
           "base_url": "https://ai.api.cloud.yandex.net/v1",
           "config_key": "YANDEX_API_KEY", "config_key2": "YANDEX_FOLDER_ID",
           "models": [{"id": "deepseek-v4.1-flash"}]}
    mock_resp = _mock_response(FAILED_BODY)
    with patch("core.api_layer.get_services", return_value={"YandexAI": svc}), \
         patch("core.api_layer.requests.post", return_value=mock_resp):
        ok, msg = test_connection("YandexAI", _yandex_cfg())

    assert ok is False
    assert "invalid_prompt" in msg
    assert "Error while calling model" in msg


def test_test_connection_reports_failed_body_deepseek():
    """The DeepSeek branch of the connection test does the same."""
    from core.api_layer import test_connection

    mock_resp = _mock_response(FAILED_BODY)
    with patch("core.api_layer.get_services",
               return_value={"DeepSeek": _deepseek_service()}), \
         patch("core.api_layer.requests.post", return_value=mock_resp):
        ok, msg = test_connection("DeepSeek", {"DEEPSEEK_API_KEY": "sk-test"})

    assert ok is False
    assert "invalid_prompt" in msg


# --- Scenario 4 - the assistant tool loop surfaces the failure --------------


def test_tool_loop_raises_on_failed_status():
    """Given a failed body, when the assistant function-call loop posts,
    then ProviderResponseError is raised after a single attempt."""
    from core.assistant_tools import run_yandex_responses_tool_loop

    mock_resp = _mock_response(FAILED_BODY)
    with patch("core.assistant_tools.requests.post",
               return_value=mock_resp) as post:
        with pytest.raises(ProviderResponseError) as excinfo:
            run_yandex_responses_tool_loop(
                "https://ai.api.cloud.yandex.net/v1", "iam-token", "folder-id",
                "deepseek-v4.1-flash", "sys", [], "вопрос",
                temperature=0.3, tools_list=[_FUNCTION_TOOL],
                cfg={}, svc_name="YandexAI",
            )

    assert post.call_count == 1
    assert "Error while calling model" in str(excinfo.value)
