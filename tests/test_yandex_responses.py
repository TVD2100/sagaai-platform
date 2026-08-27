# -*- coding: utf-8 -*-
"""
Tests for the Yandex Responses API integration (auth_type == "yandex_iam").

Yandex AI Studio models are now ALL served through POST /responses with:
- model URI ``gpt://<folder_id>/<model>`` (no ``/latest`` suffix);
- system prompt in ``instructions``;
- output tokens counting from ``usage.input_tokens`` / ``usage.output_tokens``;
- cached input tokens from ``usage.input_tokens_details.cached_tokens``;
- optional ``reasoning.effort`` and web_search ``filters``/``search_context_size``;
- optional ``tool_choice`` to force a tool (e.g. ``{"type": "web_search"}``).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


# ─── payload helpers ────────────────────────────────────────────────────────

_YANDEX_SVC = {
    "name": "YandexAI",
    "auth_type": "yandex_iam",
    "base_url": "https://ai.api.cloud.yandex.net/v1",
    "config_key": "YANDEX_API_KEY",
    "config_key2": "YANDEX_FOLDER_ID",
    "models": [{"id": "yandexgpt-5.1", "max_tokens": 32000}],
}


def _yandex_cfg(**overrides):
    cfg = {"YANDEX_API_KEY": "iam-token", "YANDEX_FOLDER_ID": "folder-id"}
    cfg.update(overrides)
    return cfg


def _responses_payload(model_uri="gpt://folder-id/yandexgpt-5.1",
                       instructions="sys", usage_tokens=(10, 20), cached=0):
    usage = {"input_tokens": usage_tokens[0], "output_tokens": usage_tokens[1],
             "total_tokens": usage_tokens[0] + usage_tokens[1]}
    if cached:
        usage["input_tokens_details"] = {"cached_tokens": cached}
    return {
        "output": [
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Ответ Яндекса"}]},
        ],
        "usage": usage,
    }


# ─── _yandex_responses_request payload ─────────────────────────────────────

def test_yandex_responses_request_payload():
    from core.api_layer import _yandex_responses_request

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload(usage_tokens=(10, 20))

    usage = {}
    with patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        result = _yandex_responses_request(
            "https://ai.api.cloud.yandex.net/v1",
            "iam-token",
            "folder-id",
            "yandexgpt-5.1",
            "system prompt",
            [{"role": "assistant", "content": "prev"}],
            "user msg",
            temperature=0.3,
            max_tokens=32000,
            tools_list=["web_search"],
            max_tool_calls=2,
            cfg=_yandex_cfg(),
            svc_name="YandexAI",
            usage_callback=lambda u: usage.update(u),
        )

    assert result == "Ответ Яндекса"
    url, kwargs = post.call_args[0][0], post.call_args[1]
    assert url == "https://ai.api.cloud.yandex.net/v1/responses"
    assert kwargs["headers"]["Authorization"] == "Bearer iam-token"

    payload = kwargs["json"]
    assert payload["model"] == "gpt://folder-id/yandexgpt-5.1"
    assert "/latest" not in payload["model"]
    assert payload["instructions"] == "system prompt"
    assert payload["temperature"] == 0.3
    assert payload["max_output_tokens"] == 32000
    assert payload["stream"] is False
    assert payload["max_tool_calls"] == 2
    assert payload["input"][0] == {"role": "assistant", "content": "prev"}
    assert payload["input"][-1] == {"role": "user", "content": "user msg"}
    assert payload["tools"][0]["type"] == "web_search"
    assert usage == {"in": 10, "out": 20, "cache": 0}


def test_yandex_responses_request_without_tools_and_system():
    from core.api_layer import _yandex_responses_request

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload(usage_tokens=(1, 2))

    with patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        _yandex_responses_request(
            "https://ai.api.cloud.yandex.net/v1",
            "iam-token",
            "folder-id",
            "yandexgpt-5.1",
            "",
            [],
            "hi",
            temperature=0.5,
            cfg=_yandex_cfg(),
            svc_name="YandexAI",
        )
    payload = post.call_args[1]["json"]
    assert "instructions" not in payload
    assert "tools" not in payload
    assert "reasoning" not in payload
    assert "tool_choice" not in payload


# ─── tool_choice forcing ───────────────────────────────────────────────────

def test_yandex_tool_choice_in_payload():
    """tool_choice is forwarded to the request payload verbatim."""
    from core.api_layer import _yandex_responses_request

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload()

    with patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        _yandex_responses_request(
            "https://ai.api.cloud.yandex.net/v1", "k", "f", "m", "", [], "q",
            temperature=0.3, tools_list=["web_search"],
            cfg=_yandex_cfg(), svc_name="YandexAI",
            tool_choice={"type": "web_search"},
        )
    assert post.call_args[1]["json"]["tool_choice"] == {"type": "web_search"}


def test_yandex_tool_choice_omitted_by_default():
    """No tool_choice means the key is not present at all."""
    from core.api_layer import _yandex_responses_request

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload()

    with patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        _yandex_responses_request(
            "https://ai.api.cloud.yandex.net/v1", "k", "f", "m", "", [], "q",
            temperature=0.3, tools_list=["web_search"],
            cfg=_yandex_cfg(), svc_name="YandexAI",
        )
    assert "tool_choice" not in post.call_args[1]["json"]


# ─── usage token counting ──────────────────────────────────────────────────

def test_yandex_uses_responses_usage_fields():
    """usage.input_tokens/output_tokens are used for token accounting."""
    from core.api_layer import _yandex_responses_request

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "x"}]},
        ],
        # Old wrong names must NOT be picked up.
        "usage": {"input_text_tokens": 999, "completion_tokens": 999,
                  "input_tokens": 42, "output_tokens": 24},
    }

    usage = {}
    with patch("core.api_layer.requests.post", return_value=mock_resp):
        _yandex_responses_request(
            "https://ai.api.cloud.yandex.net/v1", "k", "f", "m", "", [], "q",
            temperature=0.3, cfg=_yandex_cfg(), svc_name="YandexAI",
            usage_callback=lambda u: usage.update(u),
        )
    assert usage == {"in": 42, "out": 24, "cache": 0}


def test_yandex_reports_cached_tokens():
    """usage.input_tokens_details.cached_tokens becomes {"cache": N}."""
    from core.api_layer import _yandex_responses_request

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    body = _responses_payload(usage_tokens=(1000, 50))
    body["usage"]["input_tokens_details"] = {"cached_tokens": 830}
    mock_resp.json.return_value = body

    usage = {}
    with patch("core.api_layer.requests.post", return_value=mock_resp):
        _yandex_responses_request(
            "https://ai.api.cloud.yandex.net/v1", "k", "f", "m", "", [], "q",
            temperature=0.3, cfg=_yandex_cfg(), svc_name="YandexAI",
            usage_callback=lambda u: usage.update(u),
        )
    assert usage == {"in": 1000, "out": 50, "cache": 830}


def test_yandex_cached_tokens_zero_when_absent():
    """No input_tokens_details -> cache defaults to 0."""
    from core.api_layer import _yandex_responses_request

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload(usage_tokens=(33, 7))

    usage = {}
    with patch("core.api_layer.requests.post", return_value=mock_resp):
        _yandex_responses_request(
            "https://ai.api.cloud.yandex.net/v1", "k", "f", "m", "", [], "q",
            temperature=0.3, cfg=_yandex_cfg(), svc_name="YandexAI",
            usage_callback=lambda u: usage.update(u),
        )
    assert usage == {"in": 33, "out": 7, "cache": 0}


# ─── reasoning effort ──────────────────────────────────────────────────────

def test_yandex_reasoning_effort_in_payload():
    from core.api_layer import _yandex_responses_request

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload()

    with patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        _yandex_responses_request(
            "https://ai.api.cloud.yandex.net/v1", "k", "f", "m", "", [], "q",
            temperature=0.3,
            cfg=_yandex_cfg(**{"YandexAI_reasoning_effort": "high"}),
            svc_name="YandexAI",
        )
    assert post.call_args[1]["json"]["reasoning"] == {"effort": "high"}


def test_yandex_reasoning_effort_invalid_omitted():
    from core.api_layer import _yandex_responses_request

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload()

    with patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        _yandex_responses_request(
            "https://ai.api.cloud.yandex.net/v1", "k", "f", "m", "", [], "q",
            temperature=0.3,
            cfg=_yandex_cfg(**{"YandexAI_reasoning_effort": "bogus"}),
            svc_name="YandexAI",
        )
    assert "reasoning" not in post.call_args[1]["json"]


# ─── web_search enrichment ─────────────────────────────────────────────────

def test_yandex_web_search_filters_and_context_size():
    from core.api_layer import _yandex_responses_request

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload()

    cfg = _yandex_cfg(**{
        "YandexAI_web_search_context_size": "high",
        "YandexAI_web_search_allowed_domains": "docs.python.org, developer.mozilla.org",
    })
    with patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        _yandex_responses_request(
            "https://ai.api.cloud.yandex.net/v1", "k", "f", "m", "", [], "q",
            temperature=0.3, tools_list=["web_search"],
            cfg=cfg, svc_name="YandexAI",
        )
    tool = post.call_args[1]["json"]["tools"][0]
    assert tool["type"] == "web_search"
    assert tool["search_context_size"] == "high"
    assert tool["filters"]["allowed_domains"] == ["docs.python.org", "developer.mozilla.org"]


def test_yandex_web_search_defaults_when_unset():
    from core.api_layer import _yandex_responses_request

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload()

    with patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        _yandex_responses_request(
            "https://ai.api.cloud.yandex.net/v1", "k", "f", "m", "", [], "q",
            temperature=0.3, tools_list=["web_search"],
            cfg=_yandex_cfg(), svc_name="YandexAI",
        )
    tool = post.call_args[1]["json"]["tools"][0]
    assert tool["search_context_size"] == "medium"
    assert "filters" not in tool


# ─── send_request routing ──────────────────────────────────────────────────

def test_send_request_yandex_always_uses_responses():
    from core.api_layer import send_request

    skill = {
        "service": "YandexAI",
        "model": "qwen3.6-35b-a3b",
        "temperature": 0.3,
        "text": "sys",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload()

    with patch("core.api_layer.get_services", return_value={"YandexAI": _YANDEX_SVC}), \
         patch("core.api_layer.load_config", return_value=_yandex_cfg()), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        result = send_request("Привет", skill)

    assert result == "Ответ Яндекса"
    url = post.call_args[0][0]
    assert url == "https://ai.api.cloud.yandex.net/v1/responses"
    assert post.call_args[1]["json"]["instructions"] == "sys"


def test_send_request_yandex_forward_tool_choice():
    """assistant['tool_choice'] reaches the Yandex request payload."""
    from core.api_layer import send_request

    skill = {
        "service": "YandexAI",
        "model": "qwen3.6-35b-a3b",
        "temperature": 0.3,
        "text": "sys",
        "tools": [{"type": "web_search"}],
        "tool_choice": {"type": "web_search"},
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload()

    with patch("core.api_layer.get_services", return_value={"YandexAI": _YANDEX_SVC}), \
         patch("core.api_layer.load_config", return_value=_yandex_cfg()), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        send_request("Привет", skill)

    assert post.call_args[1]["json"]["tool_choice"] == {"type": "web_search"}


def test_send_request_yandex_auto_forces_web_search_tool_choice():
    """Yandex assistant with a web_search tool gets tool_choice forced automatically.

    aliceai-* models may answer with placeholder text instead of actually
    calling web_search when tool_choice is \"auto\". So for yandex_iam+web_search
    and no explicit tool_choice the platform forces the search (the same
    mechanism the DevAgent orchestrator web_search tool uses).
    """
    from core.api_layer import send_request

    skill = {
        "service": "YandexAI",
        "model": "aliceai-llm",
        "temperature": 0.7,
        "text": "sys",
        "tools": ["web_search"],
        "max_tool_calls": 1,
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload()

    with patch("core.api_layer.get_services", return_value={"YandexAI": _YANDEX_SVC}), \
         patch("core.api_layer.load_config", return_value=_yandex_cfg()), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        send_request("Какая сегодня погода в Москве?", skill)

    assert post.call_args[1]["json"]["tool_choice"] == {"type": "web_search"}


def test_send_request_yandex_auto_tool_choice_with_dict_tool():
    """Same auto-forcing works when the tool is declared as a dict."""
    from core.api_layer import send_request

    skill = {
        "service": "YandexAI",
        "model": "aliceai-llm",
        "temperature": 0.7,
        "text": "sys",
        "tools": [{"type": "web_search"}],
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload()

    with patch("core.api_layer.get_services", return_value={"YandexAI": _YANDEX_SVC}), \
         patch("core.api_layer.load_config", return_value=_yandex_cfg()), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        send_request("Привет", skill)

    assert post.call_args[1]["json"]["tool_choice"] == {"type": "web_search"}


def test_send_request_yandex_explicit_tool_choice_untouched():
    """An explicitly configured tool_choice is forwarded verbatim, not overridden."""
    from core.api_layer import send_request

    skill = {
        "service": "YandexAI",
        "model": "aliceai-llm",
        "temperature": 0.7,
        "text": "sys",
        "tools": ["web_search"],
        "tool_choice": {"type": "web_search", "custom_flag": True},
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload()

    with patch("core.api_layer.get_services", return_value={"YandexAI": _YANDEX_SVC}), \
         patch("core.api_layer.load_config", return_value=_yandex_cfg()), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        send_request("Привет", skill)

    assert post.call_args[1]["json"]["tool_choice"] == {"type": "web_search", "custom_flag": True}


def test_send_request_yandex_no_web_search_no_forced_tool_choice():
    """Assistants without a web_search tool are unaffected."""
    from core.api_layer import send_request

    skill = {
        "service": "YandexAI",
        "model": "qwen3.6-35b-a3b",
        "temperature": 0.3,
        "text": "sys",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _responses_payload()

    with patch("core.api_layer.get_services", return_value={"YandexAI": _YANDEX_SVC}), \
         patch("core.api_layer.load_config", return_value=_yandex_cfg()), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        send_request("Привет", skill)

    assert "tool_choice" not in post.call_args[1]["json"]


# ─── test_connection routing ───────────────────────────────────────────────

def test_test_connection_yandex_uses_responses():
    from core.api_layer import test_connection

    svc = dict(_YANDEX_SVC)
    svc["models"] = [{"id": "yandexgpt-5.1"}]
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("core.api_layer.get_services", return_value={"YandexAI": svc}), \
         patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        ok, msg = test_connection("YandexAI", _yandex_cfg())

    assert ok is True
    assert "200" in msg
    url = post.call_args[0][0]
    assert url == "https://ai.api.cloud.yandex.net/v1/responses"
    payload = post.call_args[1]["json"]
    assert payload["model"] == "gpt://folder-id/yandexgpt-5.1"
    assert "/latest" not in payload["model"]
    assert payload["input"] == [{"role": "user", "content": "ping"}]


def test_test_connection_yandex_missing_credentials():
    from core.api_layer import test_connection

    svc = dict(_YANDEX_SVC)
    with patch("core.api_layer.get_services", return_value={"YandexAI": svc}), \
         patch("core.api_layer.requests.post") as post:
        ok, msg = test_connection("YandexAI", _yandex_cfg(**{"YANDEX_API_KEY": ""}))
    assert ok is False
    assert "token" in msg.lower()
    post.assert_not_called()


def test_test_connection_yandex_missing_folder():
    from core.api_layer import test_connection

    svc = dict(_YANDEX_SVC)
    with patch("core.api_layer.get_services", return_value={"YandexAI": svc}), \
         patch("core.api_layer.requests.post") as post:
        ok, msg = test_connection("YandexAI", _yandex_cfg(**{"YANDEX_FOLDER_ID": ""}))
    assert ok is False
    assert "folder" in msg.lower()
    post.assert_not_called()


# ─── unified Responses extractor (all AI Studio models) ────────────────────


def test_extract_responses_text_prefers_output_text():
    """Top-level output_text (SDK convenience property) wins when present."""
    from core.api_layer import _extract_responses_text
    data = {
        "output_text": "SDK convenience text",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "ignored"}]},
        ],
    }
    assert _extract_responses_text(data) == "SDK convenience text"


def test_extract_responses_text_falls_back_to_output():
    """REST responses without output_text are parsed from output[]."""
    from core.api_layer import _extract_responses_text
    data = {
        "output": [
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Текст ответа"}]},
        ],
    }
    assert _extract_responses_text(data) == "Текст ответа"


def test_extract_responses_text_content_as_string():
    """Some compatible variants put a plain string into message.content."""
    from core.api_layer import _extract_responses_text
    data = {"output": [{"type": "message", "content": "Простой строковый контент"}]}
    assert _extract_responses_text(data) == "Простой строковый контент"


def test_extract_responses_text_ignores_reasoning_and_web_search_call():
    """reasoning / web_search_call items never leak into the result."""
    from core.api_layer import _extract_responses_text
    data = {
        "output": [
            {"type": "reasoning", "summary": [{"text": "secret chain of thought"}]},
            {"type": "web_search_call", "action": {"query": "погода", "type": "search"}},
            {"type": "message", "content": [{"type": "output_text", "text": "Финальный ответ"}]},
        ],
    }
    result = _extract_responses_text(data)
    assert result == "Финальный ответ"
    assert "secret" not in result


def test_extract_responses_text_skips_blank_blocks():
    """Multiple message items: blank/empty content blocks are skipped."""
    from core.api_layer import _extract_responses_text
    data = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "   "}]},
            {"type": "message", "content": [{"type": "output_text", "text": "Первый"}]},
            {"type": "message", "content": []},
            {"type": "message", "content": [{"type": "output_text", "text": ""}]},
            {"type": "message", "content": [{"type": "output_text", "text": "Второй"}]},
        ],
    }
    assert _extract_responses_text(data) == "Первый\n\nВторой"


def test_extract_responses_text_function_call_fenced():
    """Native function_call items become fenced JSON for the agent loop."""
    from core.api_layer import _extract_responses_text
    data = {"output": [
        {"type": "function_call", "name": "read_file",
         "arguments": '{"path": "x.py"}'},
    ]}
    result = _extract_responses_text(data)
    assert result.startswith("```json")
    assert "read_file" in result
    assert "x.py" in result


def test_extract_responses_text_incomplete_status_still_returns_text():
    """status=incomplete (max_output_tokens) keeps the partial text."""
    from core.api_layer import _extract_responses_text
    data = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "Частичный ответ"}]}],
    }
    assert _extract_responses_text(data) == "Частичный ответ"


def test_extract_responses_text_empty_output():
    from core.api_layer import _extract_responses_text
    assert _extract_responses_text({}) == ""
    assert _extract_responses_text({"output": []}) == ""
    assert _extract_responses_text({"output_text": "   "}) == ""


def test_extract_deepseek_wraps_unified_extractor():
    """DeepSeek uses the same unified parser (reasoning never leaks)."""
    from core.api_layer import _extract_deepseek_responses_text
    data = {
        "output": [
            {"type": "reasoning", "summary": [{"text": "thinking"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "Ответ DeepSeek"}]},
        ],
    }
    assert _extract_deepseek_responses_text(data) == "Ответ DeepSeek"
