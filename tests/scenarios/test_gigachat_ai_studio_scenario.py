# -*- coding: utf-8 -*-
"""tests/scenarios/test_gigachat_ai_studio_scenario.py - user-level scenario
tests for the GigaChat AI Studio provider.

The provider is an OpenAI-compatible gateway: one bearer key, no OAuth
handshake and no folder id. These scenarios walk it the way a user does -
pick the service, pick a model from the catalog, send a message, let a tool
run, test the connection - and pin the catalog shape the settings screens
rely on.

Scenarios (given -> when -> then):

  Scenario 1 - both service copies carry the same definition: the bearer
               chat endpoint, the single API key and the seven models of
               the gateway with the context windows the provider publishes.
  Scenario 2 - only the models that accept an image are offered as vision
               models, and the gateway's missing catalogs stay empty.
  Scenario 3 - sending a message posts to the AI Studio endpoint with a
               bearer header and carries the selected model id.
  Scenario 4 - a tool call runs through the native chat tool loop: the
               first round returns tool_calls, the second returns the
               answer built from the tool result.
  Scenario 5 - the connection test probes the chat endpoint with the stored
               key and reports the HTTP status.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.services import get_image_models, get_services, get_vision_models

ROOT = Path(__file__).resolve().parent.parent.parent

SERVICE_PATHS = (
    "services/gigachat_ai_studio.json",
    "defaults/services/gigachat_ai_studio.json",
)
SERVICE_NAME = "gigachat_ai_studio"
ENDPOINT = "https://api.aistudio.giga.chat/v1/chat/completions"
MODEL = "GigaChat-4-708B-A41B"


def _json(path: str) -> dict:
    """Load one repository JSON file relative to the project root."""
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _cfg() -> dict:
    """Minimal runtime config matching the service config key."""
    return {"GIGACHAT_AI_STUDIO_API_KEY": "ai-studio-key"}


def _ok_response(text: str = "Ответ") -> MagicMock:
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


# --- Scenario 1 - the catalog both service copies ship ----------------------


def test_service_copies_offer_the_ai_studio_catalog():
    """Given the service definitions, when both copies are loaded, then they
    are identical, the transport is a plain bearer chat endpoint with one
    config key, and the catalog lists the seven gateway models."""
    copies = {}
    for path in SERVICE_PATHS:
        svc = _json(path)
        copies[path] = svc
        assert svc["name"] == SERVICE_NAME, path
        assert svc["base_url"] == ENDPOINT, path
        assert svc["auth_type"] == "bearer", path
        assert svc["config_key"] == "GIGACHAT_AI_STUDIO_API_KEY", path
        assert svc.get("config_key2") is None, path
        assert svc["require_folder_id"] is False, path

        ids = [m["id"] for m in svc["models"]]
        assert len(ids) == 7, path
        assert ids[0] == MODEL, path

    assert copies[SERVICE_PATHS[0]] == copies[SERVICE_PATHS[1]], \
        "service copies diverged"


def test_every_model_declares_its_limits():
    """Given the catalog, when a model is picked, then every entry declares
    a window and an output limit of a tenth of it. Four windows come from the
    provider's published catalog; the three checkpoints that catalog does not
    list keep the windows established by probing the gateway."""
    for path in SERVICE_PATHS:
        svc = _json(path)
        assert svc.get("context_window_default"), path
        assert svc.get("max_tokens_default"), path
        windows = set()
        for model in svc["models"]:
            ctx = model.get("context_window")
            assert ctx, (path, model)
            assert model.get("max_tokens"), (path, model)
            assert model["max_tokens"] == ctx // 10, (path, model)
            windows.add(ctx)
        assert len(windows) > 1, (path, windows)
        assert svc["max_tokens_default"] == svc["context_window_default"] // 10, path


# --- Scenario 2 - the multimodal catalogs ----------------------------------


def test_only_image_capable_models_are_offered_as_vision_models():
    """Given the shipped catalog, when the vision list is read, then it holds
    exactly the models that accepted an image, and the generation and
    vectorization catalogs stay empty - the gateway serves neither."""
    svc = get_services().get(SERVICE_NAME) or {}
    assert svc, "the catalog is not registered"
    vision = [m.get("id") for m in get_vision_models(svc)]
    assert vision == [
        "GigaChat-4-708B-A41B",
        "GigaChat-3.5-432B-A28B",
        "GigaChat-3-Ultra",
        "GigaChat-3-Pro",
    ]
    # The reasoning-only and small checkpoints reject images.
    for model_id in ("GigaChat-3.5-Ultra-Reasoning", "GigaChat-4-37B-A4B",
                     "GigaChat-3-Lightning"):
        assert model_id not in vision
    assert get_image_models(svc) == []
    for entry in get_vision_models(svc):
        label = entry.get("label") or {}
        assert label.get("ru"), entry
        assert label.get("en"), entry


# --- Scenario 3 - sending a message -----------------------------------------


def test_sending_with_an_ai_studio_model_uses_the_bearer_endpoint():
    """Given the real service definition, when a message is sent with the
    GigaChat 4 model, then the request posts to the AI Studio endpoint with
    a bearer header and the payload carries the selected model id."""
    from core.api_layer import send_request

    svc = _json("services/gigachat_ai_studio.json")
    with patch("core.api_layer.get_services", return_value={SERVICE_NAME: svc}), \
         patch("core.api_layer.load_config", return_value=_cfg()), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post") as mock_post:
        mock_post.return_value = _ok_response()

        result = send_request("Привет", {
            "service": SERVICE_NAME, "model": MODEL,
            "temperature": 0.3, "text": "System prompt.", "id": "scenario",
        })

        assert result == "Ответ"
        assert mock_post.call_args[0][0] == ENDPOINT
        headers = mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer ai-studio-key"
        payload = mock_post.call_args[1]["json"]
        assert payload["model"] == MODEL


def test_sending_without_a_key_reports_the_missing_credential():
    """Given no key, when a message is sent, then the failure names the
    credential instead of surfacing a provider HTTP error."""
    from core.api_errors import ApiKeyMissingError
    from core.api_layer import send_request

    svc = _json("services/gigachat_ai_studio.json")
    with patch("core.api_layer.get_services", return_value={SERVICE_NAME: svc}), \
         patch("core.api_layer.load_config", return_value={}), \
         patch("core.api_layer.load_skill_files_context", return_value=""):
        try:
            send_request("Привет", {"service": SERVICE_NAME, "model": MODEL,
                                    "temperature": 0.3, "text": "sys", "id": "s"})
        except ApiKeyMissingError as exc:
            assert SERVICE_NAME in str(exc)
        else:
            raise AssertionError("a missing key must be reported")


# --- Scenario 4 - the native tool loop --------------------------------------


def test_a_tool_call_runs_through_the_native_chat_loop():
    """Given a function tool, when the model asks for it, then the loop runs
    two rounds: the answer carries the nested OpenAI tool shape and the
    second round sends the tool result back before the final text."""
    from core.api_layer import send_request

    svc = _json("services/gigachat_ai_studio.json")
    tool_round = MagicMock()
    tool_round.ok = True
    tool_round.status_code = 200
    tool_round.json.return_value = {"choices": [{"finish_reason": "tool_calls",
                                                  "message": {
        "content": None,
        "tool_calls": [{"id": "call-1", "type": "function",
                         "function": {"name": "get_weather",
                                      "arguments": "{\"city\": \"Москва\"}"}}],
    }}]}
    final_round = _ok_response("В Москве +18")

    with patch("core.api_layer.get_services", return_value={SERVICE_NAME: svc}), \
         patch("core.api_layer.load_config", return_value=_cfg()), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post",
               side_effect=[tool_round, final_round]) as mock_post:
        result = send_request("Погода?", {
            "service": SERVICE_NAME, "model": MODEL, "temperature": 0.3,
            "text": "sys", "id": "tool-scenario",
            "tools": [{"type": "function", "name": "get_weather",
                       "description": "Погода в городе",
                       "parameters": {"type": "object",
                                      "properties": {"city": {"type": "string"}}}}],
            "max_tool_calls": 3,
        })

    assert result == "В Москве +18"
    assert mock_post.call_count == 2
    first = mock_post.call_args_list[0].kwargs["json"]
    assert first["tools"][0]["type"] == "function"
    assert "function" in first["tools"][0], "tools must use the nested chat shape"
    second = mock_post.call_args_list[1].kwargs["json"]
    assert second["messages"][-1]["role"] == "tool"


# --- Scenario 5 - connection test -------------------------------------------


def test_connection_test_probes_the_chat_endpoint():
    """Given the real service definition, when the UI connection test runs,
    then it posts a probe to the chat endpoint with the stored key and
    reports the HTTP status."""
    from core.api_layer import test_connection

    svc = _json("services/gigachat_ai_studio.json")
    with patch("core.api_layer.get_services", return_value={SERVICE_NAME: svc}), \
         patch("core.api_layer.requests.post") as mock_post:
        resp = MagicMock()
        resp.status_code = 200
        mock_post.return_value = resp

        ok, msg = test_connection(SERVICE_NAME, _cfg())

        assert ok is True
        assert "OK" in msg
        assert mock_post.call_args[0][0] == ENDPOINT
        headers = mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer ai-studio-key"


def test_connection_test_reports_a_rejected_key():
    """Given a rejected key, when the connection test runs, then the answer
    is a failure carrying the provider status."""
    from core.api_layer import test_connection

    svc = _json("services/gigachat_ai_studio.json")
    with patch("core.api_layer.get_services", return_value={SERVICE_NAME: svc}), \
         patch("core.api_layer.requests.post") as mock_post:
        resp = MagicMock()
        resp.status_code = 401
        resp.text = "invalid_api_key"
        mock_post.return_value = resp

        ok, msg = test_connection(SERVICE_NAME, _cfg())

        assert ok is False
        assert "401" in msg

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
