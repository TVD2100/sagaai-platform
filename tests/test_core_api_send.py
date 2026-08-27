"""
tests.test_core_api_send - integration-level tests for core.api_layer.send_request, _bearer_request, etc.

These tests require mocking of network calls (requests.post, etc.).
They are kept separate from test_core_api_layer.py to ensure each test
function runs in a clean interpreter state without module-level cache
interference.
"""

import json
import sys
import pytest
from unittest.mock import patch, MagicMock
import requests as real_requests

from core.api_errors import (
    ApiKeyMissingError,
    ServiceNotFoundError,
    AuthTypeUnknownError,
    ProviderHTTPError,
    RequestTimeoutError,
    NetworkError,
)


def _make_svc(name="DeepSeek", auth_type="bearer", base_url="https://api.deepseek.com/v1/chat/completions",
              config_key="deepseek_key"):
    """Helper: build a minimal service dict."""
    return {
        name: {
            "auth_type": auth_type,
            "base_url": base_url,
            "config_key": config_key,
            "temp_default": 0.7,
        }
    }


def _make_cfg(deepseek_key="sk-test-key", yandex_iam_token="iam-token", yandex_folder_id="folder-id",
              gigachat_creds="creds-base64"):
    """Helper: build a minimal config dict."""
    return {
        "deepseek_key": deepseek_key,
        "yandex_iam_token": yandex_iam_token,
        "yandex_cloud_id": yandex_folder_id,
        "gigachat_creds": gigachat_creds,
    }


def _make_skill(service="DeepSeek", model="deepseek-chat", temperature=0.3, text="System prompt.",
                tools=None, max_tool_calls=None, skill_id="abc123"):
    """Helper: build a minimal skill dict."""
    d = {
        "service": service,
        "model": model,
        "temperature": temperature,
        "text": text,
        "id": skill_id,
    }
    if tools is not None:
        d["tools"] = tools
    if max_tool_calls is not None:
        d["max_tool_calls"] = max_tool_calls
    return d


# ─── _bearer_request ────────────────────────────────────────────────────────

def test_bearer_request_success():
    from core.api_layer import _bearer_request
    with patch("core.api_layer.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Привет, как дела?"}}]
        }
        mock_post.return_value = mock_resp

        result = _bearer_request(
            url="https://api.example.com/v1/chat/completions",
            api_key="sk-test",
            model="test-model",
            messages=[{"role": "user", "content": "Hi"}],
            temperature=0.7,
        )
        assert result == "Привет, как дела?"
        call_args = mock_post.call_args
        assert call_args[1]["headers"]["Authorization"] == "Bearer sk-test"
        sent_json = call_args[1]["json"]
        assert sent_json["model"] == "test-model"
        assert sent_json["temperature"] == 0.7
        assert "tools" not in sent_json


def test_bearer_request_http_error():
    from core.api_layer import _bearer_request
    with patch("core.api_layer.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"error": {"message": "Invalid API key"}}
        mock_post.return_value = mock_resp

        with pytest.raises(ProviderHTTPError) as exc_info:
            _bearer_request(
                url="https://api.example.com/v1/chat/completions",
                api_key="bad-key",
                model="test-model",
                messages=[],
                temperature=0.7,
            )
        assert exc_info.value.status_code == 401
        assert "Invalid API key" in str(exc_info.value)


def test_bearer_request_with_tools():
    from core.api_layer import _bearer_request
    with patch("core.api_layer.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "", "tool_calls": []}}]
        }
        mock_post.return_value = mock_resp

        _bearer_request(
            url="https://api.example.com/v1/chat/completions",
            api_key="sk-test",
            model="test-model",
            messages=[],
            temperature=0.7,
            tools=["web_search"],
        )
        sent_json = mock_post.call_args[1]["json"]
        assert sent_json["tools"] == [{"type": "web_search"}]


# ─── _gigachat_token ────────────────────────────────────────────────────────

def test_gigachat_token_success():
    from core.api_layer import _gigachat_token
    with patch("core.api_layer.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "test-token-123"}
        mock_post.return_value = mock_resp

        token = _gigachat_token("creds-base64")
        assert token == "test-token-123"


def test_gigachat_token_http_error():
    from core.api_layer import _gigachat_token
    with patch("core.api_layer.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        mock_post.return_value = mock_resp

        with pytest.raises(ProviderHTTPError, match="Bad Request"):
            _gigachat_token("bad-creds")


# ─── send_request success/failure per auth_type ─────────────────────────────

def test_send_request_bearer_success():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post") as mock_post:
        mock_cfg.return_value = _make_cfg()
        mock_svc.return_value = _make_svc()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "Результат"}}]}
        mock_post.return_value = mock_resp

        result = send_request("Привет", _make_skill())
        assert result == "Результат"


def test_send_request_bearer_missing_key():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""):
        mock_cfg.return_value = _make_cfg(deepseek_key="")
        mock_svc.return_value = _make_svc()

        with pytest.raises(ApiKeyMissingError) as exc_info:
            send_request("Привет", _make_skill())
        assert exc_info.value.service == "DeepSeek"


def test_send_request_unknown_service():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg:
        mock_cfg.return_value = _make_cfg()
        mock_svc.return_value = {}

        with pytest.raises(ServiceNotFoundError) as exc_info:
            send_request("Привет", _make_skill(service="NonExistent"))
        assert exc_info.value.service == "NonExistent"


def test_send_request_yandex_iam_with_tools():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post") as mock_post:
        mock_cfg.return_value = _make_cfg()
        svc = _make_svc(
            name="YandexAI",
            auth_type="yandex_iam",
            base_url="https://ai.api.cloud.yandex.net/v1",
            config_key="yandex_iam_token",
        )
        svc["YandexAI"]["config_key2"] = "yandex_cloud_id"
        mock_svc.return_value = svc

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"output_text": "Поиск выполнен"}
        mock_post.return_value = mock_resp

        result = send_request("Поищи информацию", _make_skill(
            service="YandexAI",
            model="yandexgpt",
            tools=["web_search"],
        ))
        assert result == "Поиск выполнен"
        call_args = mock_post.call_args
        assert "/responses" in call_args[0][0]
        sent_json = call_args[1]["json"]
        assert "tools" in sent_json


def test_send_request_yandex_iam_without_tools():
    """Yandex without tools must still use POST /responses (no chat/completions fallback)."""
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post") as mock_post:
        mock_cfg.return_value = _make_cfg()
        svc = _make_svc(
            name="YandexAI",
            auth_type="yandex_iam",
            base_url="https://ai.api.cloud.yandex.net/v1",
            config_key="yandex_iam_token",
        )
        svc["YandexAI"]["config_key2"] = "yandex_cloud_id"
        mock_svc.return_value = svc

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"output_text": "Ответ без поиска"}
        mock_post.return_value = mock_resp

        result = send_request("Просто ответ", _make_skill(service="YandexAI", model="yandexgpt"))
        assert result == "Ответ без поиска"
        call_args = mock_post.call_args
        assert "/responses" in call_args[0][0]
        sent_json = call_args[1]["json"]
        assert "instructions" in sent_json
        assert "/latest" not in sent_json["model"]


def test_send_request_yandex_iam_missing_key():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""):
        mock_cfg.return_value = _make_cfg(yandex_iam_token="")
        svc = _make_svc(
            name="YandexAI", auth_type="yandex_iam",
            base_url="https://ai.api.cloud.yandex.net/v1",
            config_key="yandex_iam_token",
        )
        svc["YandexAI"]["config_key2"] = "yandex_cloud_id"
        mock_svc.return_value = svc

        with pytest.raises(ApiKeyMissingError):
            send_request("Привет", _make_skill(service="YandexAI"))


def test_send_request_yandex_iam_missing_folder_id():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""):
        mock_cfg.return_value = _make_cfg(yandex_folder_id="")
        svc = _make_svc(
            name="YandexAI", auth_type="yandex_iam",
            base_url="https://ai.api.cloud.yandex.net/v1",
            config_key="yandex_iam_token",
        )
        svc["YandexAI"]["config_key2"] = "yandex_cloud_id"
        mock_svc.return_value = svc

        with pytest.raises(ApiKeyMissingError):
            send_request("Привет", _make_skill(service="YandexAI"))


def test_send_request_gigachat_success():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer._gigachat_token", return_value="giga-token"), \
         patch("core.api_layer.requests.Session") as mock_session_cls:
        mock_cfg.return_value = _make_cfg()
        svc = _make_svc(
            name="GigaChat",
            auth_type="gigachat_oauth",
            base_url="https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            config_key="gigachat_creds",
        )
        svc["GigaChat"]["config_key2"] = "gigachat_scope"
        mock_svc.return_value = svc

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"choices": [{"message": {"content": "Ответ GigaChat"}}]}
        mock_session.post.return_value = mock_resp
        mock_session.headers = {}
        mock_session.verify = True
        mock_session_cls.return_value = mock_session

        result = send_request("Привет", _make_skill(service="GigaChat", model="GigaChat"))
        assert result == "Ответ GigaChat"


def test_send_request_gigachat_http_error():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer._gigachat_token", return_value="giga-token"), \
         patch("core.api_layer.requests.Session") as mock_session_cls:
        mock_cfg.return_value = _make_cfg()
        svc = _make_svc(
            name="GigaChat",
            auth_type="gigachat_oauth",
            base_url="https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            config_key="gigachat_creds",
        )
        svc["GigaChat"]["config_key2"] = "gigachat_scope"
        mock_svc.return_value = svc

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 500
        mock_resp.json.return_value = {"message": "Internal error"}
        mock_session.post.return_value = mock_resp
        mock_session.headers = {}
        mock_session.verify = True
        mock_session_cls.return_value = mock_session

        with pytest.raises(ProviderHTTPError) as exc_info:
            send_request("Привет", _make_skill(service="GigaChat", model="GigaChat"))
        assert "500" in str(exc_info.value)
        assert "Internal error" in str(exc_info.value)


def test_send_request_unknown_auth_type():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""):
        mock_cfg.return_value = _make_cfg()
        svc = _make_svc(auth_type="unknown_auth")
        mock_svc.return_value = svc

        with pytest.raises(AuthTypeUnknownError) as exc_info:
            send_request("Привет", _make_skill())
        assert "unknown_auth" in str(exc_info.value)


def test_send_request_timeout():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post") as mock_post:
        mock_cfg.return_value = _make_cfg()
        mock_svc.return_value = _make_svc()
        mock_post.side_effect = real_requests.exceptions.Timeout("Timed out")

        with pytest.raises(RequestTimeoutError):
            send_request("Привет", _make_skill())


def test_send_request_network_error():
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post") as mock_post:
        mock_cfg.return_value = _make_cfg()
        mock_svc.return_value = _make_svc()
        mock_post.side_effect = real_requests.exceptions.ConnectionError("No network")

        with pytest.raises(NetworkError):
            send_request("Привет", _make_skill())


def test_send_request_with_file_context():
    """File context is appended to user message."""
    from core.api_layer import send_request
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.load_config") as mock_cfg, \
         patch("core.api_layer.load_skill_files_context", return_value="Some material"), \
         patch("core.api_layer.requests.post") as mock_post:
        mock_cfg.return_value = _make_cfg()
        mock_svc.return_value = _make_svc()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "OK"}}]}
        mock_post.return_value = mock_resp

        result = send_request(
            "Что в файле?",
            _make_skill(),
            file_context="file content here",
        )
        assert result == "OK"


# ─── test_connection ────────────────────────────────────────────────────────

def test_test_connection_bearer_success():
    from core.api_layer import test_connection
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.requests.post") as mock_post:
        svc = _make_svc()
        svc["DeepSeek"]["models"] = [{"id": "deepseek-chat"}]
        mock_svc.return_value = svc

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        ok, msg = test_connection("DeepSeek", _make_cfg())
        assert ok is True
        assert "200" in msg


def test_test_connection_bearer_missing_key():
    from core.api_layer import test_connection
    with patch("core.api_layer.get_services") as mock_svc:
        svc = _make_svc()
        svc["DeepSeek"]["models"] = [{"id": "deepseek-chat"}]
        mock_svc.return_value = svc

        ok, msg = test_connection("DeepSeek", _make_cfg(deepseek_key=""))
        assert ok is False
        assert "key" in msg.lower()


def test_test_connection_unknown_service():
    from core.api_layer import test_connection
    with patch("core.api_layer.get_services") as mock_svc:
        mock_svc.return_value = {}
        ok, msg = test_connection("NonExistent", _make_cfg())
        assert ok is False
        assert "не найден" in msg


def test_test_connection_yandex_iam_success():
    """Yandex connection test uses POST /responses (not chat/completions)."""
    from core.api_layer import test_connection
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.requests.post") as mock_post:
        svc = _make_svc(
            name="YandexAI", auth_type="yandex_iam",
            base_url="https://ai.api.cloud.yandex.net/v1",
            config_key="yandex_iam_token",
        )
        svc["YandexAI"]["config_key2"] = "yandex_cloud_id"
        svc["YandexAI"]["models"] = [{"id": "yandexgpt-lite"}]
        mock_svc.return_value = svc

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        ok, msg = test_connection("YandexAI", _make_cfg())
        assert ok is True
        assert "200" in msg
        url = mock_post.call_args[0][0]
        assert "/responses" in url
        payload = mock_post.call_args[1]["json"]
        assert "/latest" not in payload["model"]


def test_test_connection_yandex_iam_missing_key():
    from core.api_layer import test_connection
    with patch("core.api_layer.get_services") as mock_svc:
        svc = _make_svc(
            name="YandexAI", auth_type="yandex_iam",
            base_url="https://ai.api.cloud.yandex.net/v1",
            config_key="yandex_iam_token",
        )
        svc["YandexAI"]["config_key2"] = "yandex_cloud_id"
        mock_svc.return_value = svc

        ok, msg = test_connection("YandexAI", _make_cfg(yandex_iam_token=""))
        assert ok is False
        assert "token" in msg.lower()


def test_test_connection_yandex_iam_missing_folder_id():
    from core.api_layer import test_connection
    with patch("core.api_layer.get_services") as mock_svc:
        svc = _make_svc(
            name="YandexAI", auth_type="yandex_iam",
            base_url="https://ai.api.cloud.yandex.net/v1",
            config_key="yandex_iam_token",
        )
        svc["YandexAI"]["config_key2"] = "yandex_cloud_id"
        mock_svc.return_value = svc

        ok, msg = test_connection("YandexAI", _make_cfg(yandex_folder_id=""))
        assert ok is False
        assert "folder" in msg.lower()


def test_test_connection_gigachat_success():
    from core.api_layer import test_connection
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer._gigachat_token", return_value="giga-token"), \
         patch("core.api_layer.requests.Session") as mock_session_cls:
        svc = _make_svc(
            name="GigaChat",
            auth_type="gigachat_oauth",
            base_url="https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            config_key="gigachat_creds",
        )
        svc["GigaChat"]["config_key2"] = "gigachat_scope"
        mock_svc.return_value = svc

        mock_session = MagicMock()
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_session.get.return_value = mock_get_resp
        mock_session.headers = {}
        mock_session.verify = True
        mock_session_cls.return_value = mock_session

        ok, msg = test_connection("GigaChat", _make_cfg())
        assert ok is True
        assert "OK" in msg or "token" in msg.lower()


def test_test_connection_gigachat_models_failure():
    from core.api_layer import test_connection
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer._gigachat_token", return_value="giga-token"), \
         patch("core.api_layer.requests.Session") as mock_session_cls:
        svc = _make_svc(
            name="GigaChat",
            auth_type="gigachat_oauth",
            base_url="https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            config_key="gigachat_creds",
        )
        svc["GigaChat"]["config_key2"] = "gigachat_scope"
        mock_svc.return_value = svc

        mock_session = MagicMock()
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 500
        mock_session.get.return_value = mock_get_resp
        mock_session.headers = {}
        mock_session.verify = True
        mock_session_cls.return_value = mock_session

        ok, msg = test_connection("GigaChat", _make_cfg())
        assert ok is True
        assert "Token OK" in msg


def test_test_connection_missing_service():
    from core.api_layer import test_connection
    with patch("core.api_layer.get_services") as mock_svc:
        mock_svc.return_value = {}
        ok, msg = test_connection("Unknown", {})
        assert ok is False
        assert "не найден" in msg


def test_test_connection_exception():
    from core.api_layer import test_connection
    with patch("core.api_layer.get_services") as mock_svc, \
         patch("core.api_layer.requests.post") as mock_post:
        svc = _make_svc()
        svc["DeepSeek"]["models"] = [{"id": "deepseek-chat"}]
        mock_svc.return_value = svc
        mock_post.side_effect = Exception("Unexpected error")
        ok, msg = test_connection("DeepSeek", _make_cfg())
        assert ok is False
        assert "Unexpected error" in msg


def test_test_connection_truly_unknown_auth():
    from core.api_layer import test_connection
    with patch("core.api_layer.get_services") as mock_svc:
        mock_svc.return_value = {
            "TestSvc": {
                "auth_type": "not_implemented",
                "base_url": "https://x.com",
                "config_key": "k",
                "models": [],
            }
        }
        ok, msg = test_connection("TestSvc", {"k": "v"})
        assert ok is False
        assert "Unknown auth_type" in msg
