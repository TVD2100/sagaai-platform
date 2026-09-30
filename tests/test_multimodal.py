"""
tests/test_multimodal.py - unit tests for core.multimodal (model
resolution, image loading, analyze/generate wrappers) and the multimodal
transports added to core.api_layer (vision request, synchronous
OpenAI-compatible image generation).

All provider I/O is mocked: no network access here. The live smoke check
is run separately via scripts (see the task journal).
"""
import base64
import os
import requests
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.multimodal as cm
from core.api_errors import (
    ApiKeyMissingError,
    AuthTypeUnknownError,
    ProviderHTTPError,
    RequestTimeoutError,
)


JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _jpeg(tmp_path, name="image.jpeg"):
    p = tmp_path / name
    p.write_bytes(JPEG_BYTES)
    return str(p)


def _services():
    """Service catalog mimic: YandexAI has vision+image, DeepSeek has none."""
    return {
        "YandexAI": {
            "name": "YandexAI",
            "auth_type": "yandex_iam",
            "config_key": "YANDEX_API_KEY",
            "config_key2": "YANDEX_FOLDER_ID",
            "base_url": "https://ai.api.cloud.yandex.net/v1",
            "vision_models": [{"id": "qwen3.6-35b-a3b", "label": {}}],
            "image_models": [{"id": "aliceai-image-art-3.0", "label": {}}],
        },
        "DeepSeek": {
            "name": "DeepSeek",
            "auth_type": "deepseek_responses",
            "config_key": "DEEPSEEK_API_KEY",
            "base_url": "https://api.deepseek.com/v1",
        },
    }


def _mock_response(status=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status
    resp.ok = status == 200
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    return resp


# ─── resolve_model ──────────────────────────────────────────────────────────


class TestResolveModel:
    def test_vision_not_assigned(self):
        with pytest.raises(cm.MultimodalError) as e:
            cm.resolve_model(cm.VISION, config={})
        assert e.value.code == "not_assigned"
        assert "распознавания" in str(e.value)

    def test_image_not_assigned(self):
        with pytest.raises(cm.MultimodalError) as e:
            cm.resolve_model(cm.IMAGE, config={"image_service": "YandexAI"})
        assert e.value.code == "not_assigned"
        assert "генерации" in str(e.value)

    def test_service_not_found(self):
        cfg = {"vision_service": "NoSuch", "vision_model": "m"}
        with patch("core.multimodal.get_services", return_value=_services()):
            with pytest.raises(cm.MultimodalError) as e:
                cm.resolve_model(cm.VISION, config=cfg)
        assert e.value.code == "service_not_found"

    def test_catalog_empty(self):
        cfg = {"vision_service": "DeepSeek", "vision_model": "whatever"}
        with patch("core.multimodal.get_services", return_value=_services()):
            with pytest.raises(cm.MultimodalError) as e:
                cm.resolve_model(cm.VISION, config=cfg)
        assert e.value.code == "catalog_empty"

    def test_model_not_declared(self):
        cfg = {"image_service": "YandexAI", "image_model": "sd-xl"}
        with patch("core.multimodal.get_services", return_value=_services()):
            with pytest.raises(cm.MultimodalError) as e:
                cm.resolve_model(cm.IMAGE, config=cfg)
        assert e.value.code == "model_not_declared"

    def test_ok(self):
        cfg = {"vision_service": "YandexAI", "vision_model": "qwen3.6-35b-a3b"}
        with patch("core.multimodal.get_services", return_value=_services()):
            assert cm.resolve_model(cm.VISION, config=cfg) == (
                "YandexAI", "qwen3.6-35b-a3b")

    def test_unknown_kind(self):
        with pytest.raises(ValueError):
            cm.resolve_model("audio", config={})


# ─── load_image / detect_mime ───────────────────────────────────────────────


class TestLoadImage:
    def test_ok_jpeg(self, tmp_path):
        p = _jpeg(tmp_path)
        loaded = cm.load_image(p)
        assert loaded["mime"] == "image/jpeg"
        assert base64.b64decode(loaded["data"]) == JPEG_BYTES
        assert loaded["size"] == len(JPEG_BYTES)

    def test_png_and_webp(self, tmp_path):
        png = tmp_path / "a.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
        webp = tmp_path / "a.webp"
        webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBP" + b"0")
        assert cm.load_image(str(png))["mime"] == "image/png"
        assert cm.load_image(str(webp))["mime"] == "image/webp"

    def test_not_found(self, tmp_path):
        with pytest.raises(cm.MultimodalError) as e:
            cm.load_image(str(tmp_path / "missing.jpeg"))
        assert e.value.code == "image_not_found"

    def test_empty(self, tmp_path):
        p = tmp_path / "empty.jpeg"
        p.write_bytes(b"")
        with pytest.raises(cm.MultimodalError) as e:
            cm.load_image(str(p))
        assert e.value.code == "image_empty"

    def test_too_large(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cm, "MAX_IMAGE_BYTES", 10)
        p = tmp_path / "big.jpeg"
        p.write_bytes(JPEG_BYTES)
        with pytest.raises(cm.MultimodalError) as e:
            cm.load_image(str(p))
        assert e.value.code == "image_too_large"

    def test_unsupported_format(self, tmp_path):
        p = tmp_path / "doc.pdf"
        p.write_bytes(b"%PDF-1.7 data")
        with pytest.raises(cm.MultimodalError) as e:
            cm.load_image(str(p))
        assert e.value.code == "unsupported_format"


# ─── analyze_image / generate_image wrappers ───────────────────────────────


class TestAnalyzeImage:
    def _cfg(self):
        return {"vision_service": "YandexAI", "vision_model": "qwen3.6-35b-a3b"}

    def test_ok_passes_payload_to_transport(self, tmp_path):
        p = _jpeg(tmp_path)
        with patch("core.multimodal.get_services", return_value=_services()), \
             patch("core.multimodal.send_vision_request",
                   return_value="A cat") as send:
            out = cm.analyze_image([p], "What is this?", config=self._cfg())
        assert out == {"ok": True, "service": "YandexAI",
                       "model": "qwen3.6-35b-a3b",
                       "text": "A cat", "images": 1}
        args, _kwargs = send.call_args
        assert args[0] == "YandexAI"
        assert args[1] == "qwen3.6-35b-a3b"
        assert args[2] == "What is this?"
        images = args[3]
        assert len(images) == 1
        assert images[0]["mime"] == "image/jpeg"
        assert base64.b64decode(images[0]["data"]) == JPEG_BYTES

    def test_no_images(self):
        with pytest.raises(cm.MultimodalError) as e:
            cm.analyze_image([], "x", config={})
        assert e.value.code == "no_images"

    def test_too_many_images(self):
        with pytest.raises(cm.MultimodalError) as e:
            cm.analyze_image(["a"] * 6, "x", config={})
        assert e.value.code == "too_many_images"


class TestGenerateImage:
    def _cfg(self):
        return {"image_service": "YandexAI",
                "image_model": "aliceai-image-art-3.0"}

    def test_ok(self):
        with patch("core.multimodal.get_services", return_value=_services()), \
             patch("core.multimodal.send_image_generation_request",
                   return_value={"mime": "image/jpeg", "data": "QUJD"}) as send:
            out = cm.generate_image("A cat", config=self._cfg())
        assert out == {"ok": True, "service": "YandexAI",
                       "model": "aliceai-image-art-3.0",
                       "mime": "image/jpeg", "data": "QUJD"}
        send.assert_called_once_with("YandexAI", "aliceai-image-art-3.0", "A cat")

    def test_empty_prompt(self):
        with pytest.raises(cm.MultimodalError) as e:
            cm.generate_image("   ", config=self._cfg())
        assert e.value.code == "empty_prompt"


class TestFormatApiError:
    def test_multimodal_error(self):
        err = cm.MultimodalError("not_assigned", "no model")
        assert cm.format_api_error(err) == "no model"

    def test_provider_error_mentions_status(self):
        err = ProviderHTTPError(500, "boom", service="YandexAI")
        text = cm.format_api_error(err)
        assert "500" in text and "boom" in text


# ─── api_layer transports ───────────────────────────────────────────────────


class TestSendVisionRequest:
    def test_yandex_payload(self):
        svc = _services()["YandexAI"]
        cfg = {"YANDEX_API_KEY": "iam", "YANDEX_FOLDER_ID": "folder1"}
        resp = _mock_response(200, {"choices": [{"message": {"content": "A cat"}}]})
        with patch("core.api_layer.get_services", return_value={"YandexAI": svc}), \
             patch("core.api_layer.load_config", return_value=cfg), \
             patch("core.api_layer.requests.post", return_value=resp) as post:
            from core.api_layer import send_vision_request
            text = send_vision_request(
                "YandexAI", "qwen3.6-35b-a3b", "Describe",
                [{"mime": "image/jpeg", "data": "QUJD"}],
                temperature=0.2, max_tokens=100)
        assert text == "A cat"
        url = post.call_args[0][0]
        payload = post.call_args[1]["json"]
        assert url == "https://ai.api.cloud.yandex.net/v1/chat/completions"
        assert payload["model"] == "gpt://folder1/qwen3.6-35b-a3b"
        assert payload["max_completion_tokens"] == 100
        parts = payload["messages"][0]["content"]
        assert parts[0] == {"type": "text", "text": "Describe"}
        assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64,QUJD"

    def test_missing_key(self):
        svc = _services()["YandexAI"]
        with patch("core.api_layer.get_services", return_value={"YandexAI": svc}), \
             patch("core.api_layer.load_config", return_value={}):
            from core.api_layer import send_vision_request
            with pytest.raises(ApiKeyMissingError):
                send_vision_request("YandexAI", "m", "p",
                                    [{"mime": "image/jpeg", "data": "x"}])

    def test_unknown_auth_type(self):
        svc = {"auth_type": "gigachat_oauth", "config_key": "K"}
        with patch("core.api_layer.get_services", return_value={"S": svc}):
            from core.api_layer import send_vision_request
            with pytest.raises(AuthTypeUnknownError):
                send_vision_request("S", "m", "p",
                                    [{"mime": "image/jpeg", "data": "x"}])

    def test_provider_http_error(self):
        svc = _services()["YandexAI"]
        cfg = {"YANDEX_API_KEY": "iam", "YANDEX_FOLDER_ID": "f"}
        resp = _mock_response(403, {"error": {"message": "no role"}})
        with patch("core.api_layer.get_services", return_value={"YandexAI": svc}), \
             patch("core.api_layer.load_config", return_value=cfg), \
             patch("core.api_layer.requests.post", return_value=resp):
            from core.api_layer import send_vision_request
            with pytest.raises(ProviderHTTPError) as e:
                send_vision_request("YandexAI", "m", "p",
                                    [{"mime": "image/jpeg", "data": "x"}])
        assert e.value.status_code == 403


class TestImageGenerationTransport:
    def test_yandex_images_endpoint(self):
        svc = _services()["YandexAI"]
        cfg = {"YANDEX_API_KEY": "iam", "YANDEX_FOLDER_ID": "folder1"}
        resp = _mock_response(200, {"data": [{"b64_json": "QUJD"}]})
        with patch("core.api_layer.get_services", return_value={"YandexAI": svc}), \
             patch("core.api_layer.load_config", return_value=cfg), \
             patch("core.api_layer.requests.post", return_value=resp) as post:
            from core.api_layer import send_image_generation_request
            out = send_image_generation_request(
                "YandexAI", "aliceai-image-art-3.0", "A cat")
        assert out["mime"] == "image/jpeg"
        assert out["data"] == "QUJD"
        assert out["model"] == "aliceai-image-art-3.0"
        assert post.call_args[0][0] == (
            "https://ai.api.cloud.yandex.net/v1/images/generations")
        headers = post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer iam"
        assert headers["OpenAI-Project"] == "folder1"
        assert headers["x-project"] == "folder1"
        payload = post.call_args[1]["json"]
        assert payload["model"] == "art://folder1/aliceai-image-art-3.0/latest"
        assert payload["prompt"] == "A cat"
        assert payload["response_format"] == "b64_json"

    def test_yandex_missing_folder(self):
        svc = _services()["YandexAI"]
        with patch("core.api_layer.get_services", return_value={"YandexAI": svc}), \
             patch("core.api_layer.load_config",
                   return_value={"YANDEX_API_KEY": "iam"}):
            from core.api_layer import send_image_generation_request
            with pytest.raises(ApiKeyMissingError):
                send_image_generation_request(
                    "YandexAI", "aliceai-image-art-3.0", "A cat")

    def test_timeout(self):
        svc = _services()["YandexAI"]
        cfg = {"YANDEX_API_KEY": "iam", "YANDEX_FOLDER_ID": "folder1"}
        with patch("core.api_layer.get_services", return_value={"YandexAI": svc}), \
             patch("core.api_layer.load_config", return_value=cfg), \
             patch("core.api_layer.requests.post",
                   side_effect=requests.exceptions.Timeout("slow")):
            from core.api_layer import send_image_generation_request
            with pytest.raises(RequestTimeoutError):
                send_image_generation_request(
                    "YandexAI", "aliceai-image-art-3.0", "A cat")

    def test_bearer_images_endpoint(self):
        svc = {"auth_type": "bearer", "config_key": "OPENAI_API_KEY",
               "base_url": "https://api.example.com/v1"}
        cfg = {"OPENAI_API_KEY": "k"}
        resp = _mock_response(200, {"data": [{"b64_json": "QUJD"}]})
        with patch("core.api_layer.get_services", return_value={"S": svc}), \
             patch("core.api_layer.load_config", return_value=cfg), \
             patch("core.api_layer.requests.post", return_value=resp) as post:
            from core.api_layer import send_image_generation_request
            out = send_image_generation_request("S", "img-model", "A cat")
        assert out["data"] == "QUJD"
        assert post.call_args[0][0] == "https://api.example.com/v1/images/generations"
        assert post.call_args[1]["json"]["response_format"] == "b64_json"
        assert post.call_args[1]["json"]["model"] == "img-model"
        headers = post.call_args[1]["headers"]
        assert "OpenAI-Project" not in headers
        assert "x-project" not in headers

    def test_error_payload_is_surfaced(self):
        svc = _services()["YandexAI"]
        cfg = {"YANDEX_API_KEY": "iam", "YANDEX_FOLDER_ID": "folder1"}
        resp = _mock_response(
            400, {"error": {"message": "Failed to parse model URI",
                            "type": "invalid_request_error"}})
        with patch("core.api_layer.get_services", return_value={"YandexAI": svc}), \
             patch("core.api_layer.load_config", return_value=cfg), \
             patch("core.api_layer.requests.post", return_value=resp):
            from core.api_layer import send_image_generation_request
            with pytest.raises(ProviderHTTPError) as e:
                send_image_generation_request(
                    "YandexAI", "aliceai-image-art-3.0", "A cat")
        assert "Failed to parse model URI" in str(e.value)
        assert e.value.status_code == 400


    def test_permission_denied_hint(self):
        svc = _services()["YandexAI"]
        cfg = {"YANDEX_API_KEY": "iam", "YANDEX_FOLDER_ID": "folder1"}
        resp = _mock_response(
            403, {"error": "Access to model "
                           "art://folder1/aliceai-image-art-3.0/latest denied",
                  "code": 7})
        with patch("core.api_layer.get_services", return_value={"YandexAI": svc}), \
             patch("core.api_layer.load_config", return_value=cfg), \
             patch("core.api_layer.requests.post", return_value=resp):
            from core.api_layer import send_image_generation_request
            with pytest.raises(ProviderHTTPError) as e:
                send_image_generation_request(
                    "YandexAI", "aliceai-image-art-3.0", "A cat")
        assert "ai.imageGeneration.user" in str(e.value)
        assert "folder1" in str(e.value)
