# tests.test_core_api_json_schema - structured output (native JSON Schema)
# Targeted tests for the core.api_layer structured-output plumbing.

import json
import pytest
from unittest.mock import patch, MagicMock

from core.api_errors import ProviderHTTPError

SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "is_coding_task": {"type": "boolean"},
        "required_tools": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["reasoning", "is_coding_task", "required_tools"],
    "additionalProperties": False,
}

# --- helpers -----------------------------------------------------------

def _svc(name, auth_type, base_url, config_key, config_key2=None):
    svc = {
        "auth_type": auth_type,
        "base_url": base_url,
        "config_key": config_key,
        "temp_default": 0.7,
    }
    if config_key2:
        svc["config_key2"] = config_key2
    return {name: svc}

def _cfg(**overrides):
    cfg = {
        "deepseek_key": "sk-test",
        "yandex_iam_token": "iam-token",
        "yandex_cloud_id": "folder-id",
        "gigachat_creds": "creds",
    }
    cfg.update(overrides)
    return cfg

def _skill(service, model, **extra):
    d = {
        "service": service,
        "model": model,
        "temperature": 0.3,
        "text": "System prompt.",
        "id": "abc123",
    }
    d.update(extra)
    return d

# --- payload rendering ----------------------------------------------------

def test_bearer_request_sends_openai_response_format():
    from core.api_layer import _bearer_request
    with patch("core.api_layer.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "{\"a\": 1}"}}]
        }
        mock_post.return_value = mock_resp

        _bearer_request(
            url="https://x.test/v1/chat/completions",
            api_key="sk",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.7,
            json_schema={"name": "my_result", "schema": SCHEMA},
        )

        sent = mock_post.call_args[1]["json"]
        fmt = sent["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["name"] == "my_result"
        assert fmt["json_schema"]["schema"] == SCHEMA


def test_responses_transports_render_text_format():
    from core.api_layer import _responses_json_format
    fmt = _responses_json_format({"name": "envelope", "schema": SCHEMA})
    assert fmt == {
        "type": "json_schema",
        "name": "envelope",
        "schema": SCHEMA,
        "strict": True,
    }


def test_send_request_yandex_payload_contains_text_format():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post") as mock_post:
        mock_cfg.return_value = _cfg()
        mock_svc.return_value = _svc(
            "YandexAI", "yandex_iam",
            "https://ai.api.cloud.yandex.net/v1",
            "yandex_iam_token", config_key2="yandex_cloud_id",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"output_text": "{'x': 1}"}
        mock_post.return_value = mock_resp

        send_request("Привет", _skill("YandexAI", "yandexgpt", json_schema=SCHEMA))

        sent = mock_post.call_args[1]["json"]
        fmt = sent["text"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["name"] == "structured_output"
        assert fmt["strict"] is True


def test_send_request_deepseek_payload_contains_text_format():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post") as mock_post:
        mock_cfg.return_value = _cfg()
        mock_svc.return_value = _svc(
            "DeepSeek", "deepseek_responses",
            "https://api.deepseek.com", "deepseek_key",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "output": [
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "{\"a\": 1}"}]}
            ]
        }
        mock_post.return_value = mock_resp

        send_request("Привет", _skill("DeepSeek", "deepseek-chat", json_schema=SCHEMA))

        sent = mock_post.call_args[1]["json"]
        assert "text" in sent
        assert sent["text"]["format"]["type"] == "json_schema"


def test_send_request_gigachat_payload_contains_response_format():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer._gigachat_token", return_value="giga-token"), \
         patch("core.api_layer.requests.Session") as mock_session_cls:
        mock_cfg.return_value = _cfg()
        mock_svc.return_value = _svc(
            "GigaChat", "gigachat_oauth",
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            "gigachat_creds",
        )
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
        mock_session.post.return_value = mock_resp
        mock_session.headers = {}
        mock_session.verify = True
        mock_session_cls.return_value = mock_session

        send_request("Привет", _skill("GigaChat", "GigaChat", json_schema=SCHEMA))

        sent = mock_session.post.call_args[1]["json"]
        fmt = sent["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["schema"] == SCHEMA
        assert "name" not in fmt


# --- unwrapping -----------------------------------------------------------

def test_unwrap_json_text_strips_unk_fence_and_envelope():
    from core.api_layer import _unwrap_json_text
    raw = "```json" + chr(10) + "{\"structured_output\": {\"reasoning\": \"x\"}}" + chr(10) + "```"
    out = _unwrap_json_text(raw)
    assert json.loads(out) == {"reasoning": "x"}


def test_unwrap_json_text_strips_unk_token():
    from core.api_layer import _unwrap_json_text
    out = _unwrap_json_text("<unk>{\"answer\": 42}")
    assert json.loads(out) == {"answer": 42}


def test_bearer_unwraps_gigachat_style_envelope():
    from core.api_layer import _bearer_request
    with patch("core.api_layer.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        wrapped = "```json" + chr(10) + "{\"envelope\": {\"a\": 1}}" + chr(10) + "```"
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": wrapped}}]
        }
        mock_post.return_value = mock_resp

        out = _bearer_request(
            url="https://x.test/v1/chat/completions", api_key="sk", model="m",
            messages=[], temperature=0.7, json_schema=SCHEMA,
        )
        assert json.loads(out) == {"a": 1}


# --- retry-fallback -------------------------------------------------------

def test_send_request_retries_without_schema_on_rejection():
    from core.api_layer import send_request
    calls = []

    def _fake_post(url, **kwargs):
        calls.append(kwargs["json"])
        if len(calls) == 1:
            raise ProviderHTTPError(400, "Bad Request: response_format is unsupported")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
        return mock_resp

    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post", side_effect=_fake_post):
        mock_cfg.return_value = _cfg()
        mock_svc.return_value = _svc(
            "DeepSeek", "bearer",
            "https://api.deepseek.com/v1/chat/completions", "deepseek_key",
        )
        out = send_request("Привет", _skill("DeepSeek", "deepseek-chat", json_schema=SCHEMA))
        assert out == "{}"
        assert len(calls) == 2
        assert "response_format" in calls[0]
        assert "response_format" not in calls[1]


def test_send_request_does_not_retry_on_other_errors():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post", side_effect=ProviderHTTPError(
             401, "Invalid API key")):
        mock_cfg.return_value = _cfg()
        mock_svc.return_value = _svc(
            "DeepSeek", "bearer",
            "https://api.deepseek.com/v1/chat/completions", "deepseek_key",
        )
        with pytest.raises(ProviderHTTPError):
            send_request("Привет", _skill("DeepSeek", "deepseek-chat", json_schema=SCHEMA))


def test_normalise_json_schema_envelopes():
    from core.api_layer import _normalise_json_schema
    bare = _normalise_json_schema(SCHEMA)
    assert bare == {"name": "structured_output", "schema": SCHEMA}
    env = _normalise_json_schema({"name": "my_name", "schema": SCHEMA})
    assert env == {"name": "my_name", "schema": SCHEMA}
    assert _normalise_json_schema(None) is None
    assert _normalise_json_schema("not-a-dict") is None
    assert _normalise_json_schema({"name": "x"}) == {"name": "x", "schema": {"name": "x"}}
