# -*- coding: utf-8 -*-
"""tests/scenarios/test_multimodal_scenarios.py - user-level scenarios for
the multimodal platform tools analyze_image / generate_image.

Scenarios (given -> when -> then), walking the same public entry points the
LLM uses (ToolExecutor.dispatch) with provider I/O mocked at the transport
boundary (core.api_layer.requests):

  Scenario 1 - a user attaches a photo to the dialog and asks what is on it.
               Given the active dialog thread with the uploaded JPEG, when
               the agent dispatches analyze_image with the upload name and
               a custom prompt, then the vision transport receives the
               exact file bytes as base64 with gpt://folder/model
               addressing and the tool reports the description + model.

  Scenario 2 - fresh install: no vision/image model is assigned (the
               default config). Both tools answer not_assigned with an
               actionable message and no provider request is made.

  Scenario 3 - the user assigned a model that the provider catalog does
               not declare. The tool answers model_not_declared and makes
               no provider request.

  Scenario 4 - the user asks for a picture: the tool calls the synchronous
               OpenAI-compatible Images API, the JPEG lands in the dialog
               files folder (no base64 in the tool result); the
               output_path variant saves into the project instead.

  Scenario 5 - permission denied: the provider answers 403 (the service
               account lacks the ai.imageGeneration.user role). The tool
               surfaces the role hint with the folder id and saves no file.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

TID = "scen_multimodal"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x42" * 24
IMG_B64 = base64.b64encode(JPEG_BYTES).decode("ascii")


def _services():
    """Provider catalog mimic: YandexAI declares vision + image models."""
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
    }


ASSIGNED = {
    "vision_service": "YandexAI",
    "vision_model": "qwen3.6-35b-a3b",
    "image_service": "YandexAI",
    "image_model": "aliceai-image-art-3.0",
}

YANDEX_KEYS = {"YANDEX_API_KEY": "iam-token", "YANDEX_FOLDER_ID": "folder1"}


def _resp(status=200, json_data=None):
    """Build a fake HTTP response for the transport mocks."""
    resp = MagicMock()
    resp.status_code = status
    resp.ok = status == 200
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = ""
    return resp


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated project + active dialog thread with an uploaded photo."""
    from core import paths as core_paths
    from dev_agent import config as dagent_config

    history = tmp_path / "history"
    project = tmp_path / "project"
    project.mkdir()
    files_dir = history / TID / "files"
    files_dir.mkdir(parents=True)
    upload = files_dir / "photo.jpeg"
    upload.write_bytes(JPEG_BYTES)

    monkeypatch.setattr(core_paths, "HISTORY_DIR", str(history))
    monkeypatch.setattr(dagent_config, "PROJECT_ROOT", project)
    monkeypatch.setattr(dagent_config, "ACTIVE_THREAD_ID", TID)
    return {"project": project, "files_dir": files_dir, "upload": upload}


def test_scenario_1_dialog_upload_analyzed_end_to_end(env):
    """Given the user attached photo.jpeg to the active dialog thread,
    when the agent dispatches analyze_image with the upload name and a
    custom prompt, then the vision request carries the exact file bytes
    as base64 and the tool result reports the description + model used."""
    from dev_agent.tool_executor import ToolExecutor

    resp = _resp(200, {"choices": [{"message": {"content": "A blue square."}}]})
    with patch("core.multimodal.load_devagent_config", return_value=ASSIGNED), \
         patch("core.multimodal.get_services", return_value=_services()), \
         patch("core.api_layer.get_services", return_value=_services()), \
         patch("core.api_layer.load_config", return_value=YANDEX_KEYS), \
         patch("core.api_layer.requests.post", return_value=resp) as post:
        result = ToolExecutor().dispatch(
            "analyze_image",
            {"images": "photo.jpeg", "prompt": "What is on the photo?",
             "max_tokens": 120})

    assert result["ok"] is True
    assert result["text"] == "A blue square."
    assert result["service"] == "YandexAI"
    assert result["model"] == "qwen3.6-35b-a3b"
    assert result["images"] == [str(env["upload"])]
    assert post.call_count == 1
    url = post.call_args[0][0]
    payload = post.call_args[1]["json"]
    assert url == "https://ai.api.cloud.yandex.net/v1/chat/completions"
    assert payload["model"] == "gpt://folder1/qwen3.6-35b-a3b"
    assert payload["max_completion_tokens"] == 120
    parts = payload["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "What is on the photo?"}
    assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64," + IMG_B64


def test_scenario_2_fresh_install_reports_unassigned_models(env):
    """Given the default (fresh) DevAgent settings with no vision/image
    model assigned, when both multimodal tools are dispatched, then each
    answers not_assigned with an actionable message and no provider
    request is made."""
    from core.config import _DEVAGENT_FALLBACK_DEFAULTS
    from dev_agent.tool_executor import ToolExecutor

    assert _DEVAGENT_FALLBACK_DEFAULTS.get("vision_service", "") in ("", None)
    assert _DEVAGENT_FALLBACK_DEFAULTS.get("image_service", "") in ("", None)

    te = ToolExecutor()
    with patch("core.multimodal.load_devagent_config", return_value={}), \
         patch("core.api_layer.requests.post") as post:
        vision = te.dispatch("analyze_image", {"images": "photo.jpeg"})
        image = te.dispatch("generate_image", {"prompt": "A blue square"})

    assert vision["ok"] is False
    assert vision["code"] == "not_assigned"
    assert "распознавания" in vision["error"]
    assert "DevAgent" in vision["error"]

    assert image["ok"] is False
    assert image["code"] == "not_assigned"
    assert "генерации" in image["error"]

    post.assert_not_called()


def test_scenario_3_model_not_declared_by_provider(env):
    """Given a vision model assigned in the settings that the provider
    catalog does not declare, when analyze_image is dispatched, then the
    tool answers model_not_declared and makes no provider request."""
    from dev_agent.tool_executor import ToolExecutor

    cfg = dict(ASSIGNED, vision_model="some-other-model")
    with patch("core.multimodal.load_devagent_config", return_value=cfg), \
         patch("core.multimodal.get_services", return_value=_services()), \
         patch("core.api_layer.requests.post") as post:
        result = ToolExecutor().dispatch("analyze_image",
                                         {"images": "photo.jpeg"})

    assert result["ok"] is False
    assert result["code"] == "model_not_declared"
    assert result["error"]
    post.assert_not_called()


def test_scenario_4_generate_image_saves_to_dialog_then_project(env):
    """Given the image model is assigned, when the user asks for a picture,
    then the synchronous Images API call runs, the JPEG lands in the dialog
    files folder with the exact bytes and no base64 in the result; the
    output_path variant saves into the project instead."""
    from dev_agent.tool_executor import ToolExecutor

    images_resp = _resp(200, {"data": [{"b64_json": IMG_B64}]})
    te = ToolExecutor()

    with patch("core.multimodal.load_devagent_config", return_value=ASSIGNED), \
         patch("core.multimodal.get_services", return_value=_services()), \
         patch("core.api_layer.get_services", return_value=_services()), \
         patch("core.api_layer.load_config", return_value=YANDEX_KEYS), \
         patch("core.api_layer.requests.post", return_value=images_resp) as post:
        dialog = te.dispatch("generate_image", {"prompt": "A blue square"})
        project = te.dispatch("generate_image",
                              {"prompt": "A blue square",
                               "output_path": "assets/pic"})

    assert dialog["ok"] is True
    assert "data" not in dialog
    assert dialog["service"] == "YandexAI"
    assert dialog["model"] == "aliceai-image-art-3.0"
    assert dialog["mime"] == "image/jpeg"
    saved = Path(dialog["path"])
    assert saved.parent == env["files_dir"]
    assert saved.name.startswith("generated_image_")
    assert saved.read_bytes() == JPEG_BYTES

    assert project["ok"] is True
    target = env["project"] / "assets" / "pic.jpeg"
    assert Path(project["path"]) == target
    assert target.read_bytes() == JPEG_BYTES

    assert post.call_count == 2
    assert post.call_args[0][0] == (
        "https://ai.api.cloud.yandex.net/v1/images/generations")
    payload = post.call_args[1]["json"]
    assert payload["model"] == "art://folder1/aliceai-image-art-3.0/latest"
    assert payload["response_format"] == "b64_json"
    headers = post.call_args[1]["headers"]
    assert headers["OpenAI-Project"] == "folder1"
    assert headers["x-project"] == "folder1"


def test_scenario_5_generation_permission_denied_role_hint(env):
    """Given the generated-model account lacks the ai.imageGeneration.user
    role, when the provider answers 403, then the tool surfaces the role
    hint with the folder id and saves no file."""
    from dev_agent.tool_executor import ToolExecutor

    denied = _resp(
        403, {"error": "Access to model "
                      "art://folder1/aliceai-image-art-3.0/latest denied",
              "code": 7})
    with patch("core.multimodal.load_devagent_config", return_value=ASSIGNED), \
         patch("core.multimodal.get_services", return_value=_services()), \
         patch("core.api_layer.get_services", return_value=_services()), \
         patch("core.api_layer.load_config", return_value=YANDEX_KEYS), \
         patch("core.api_layer.requests.post", return_value=denied):
        result = ToolExecutor().dispatch("generate_image",
                                         {"prompt": "A blue square"})

    assert result["ok"] is False
    assert "ai.imageGeneration.user" in result["error"]
    assert "folder1" in result["error"]
    assert list(env["files_dir"].glob("generated_image_*")) == []
