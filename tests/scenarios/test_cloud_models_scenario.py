# -*- coding: utf-8 -*-
"""tests/scenarios/test_cloud_models_scenario.py - user-level scenario tests
for the Cloud.ru Foundation Models provider.

The provider is an OpenAI-compatible gateway: one bearer key, no OAuth
handshake and no project id. These scenarios walk it the way a user does -
pick the service, pick a model from the catalog, send a message, test the
connection - and pin the catalog shape the settings screens rely on.

Scenarios (given -> when -> then):

  Scenario 1 - both service copies carry the same Cloud definition: the
               bearer endpoint, the single API key and the model catalog
               that opens with the GigaChat line and then the largest
               vendors' models.
  Scenario 2 - RAG generation recommends a GigaChat 4 model first.
  Scenario 3 - sending a message posts to the Cloud endpoint with a bearer
               header and carries the selected model id.
  Scenario 4 - the connection test probes the chat endpoint with the stored
               key and reports the HTTP status.
  Scenario 5 - the retired provider leaves no trace in the shipped catalog
               or in the transport layer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

ROOT = Path(__file__).resolve().parent.parent.parent

SERVICE_PATHS = ("services/cloud.json", "defaults/services/cloud.json")
ENDPOINT = "https://foundation-models.api.cloud.ru/v1/chat/completions"
LEGACY_HOST = "api.giga.chat"


def _json(path: str) -> dict:
    """Load one repository JSON file relative to the project root."""
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _cfg() -> dict:
    """Minimal Cloud runtime config matching the service config key."""
    return {"CLOUD_API_KEY": "cloud-key"}


def _ok_response(text: str = "Ответ") -> MagicMock:
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


# --- Scenario 1 - the catalog both service copies ship -----------------------


def test_service_copies_offer_the_cloud_catalog():
    """Given the Cloud service definitions, when both copies are loaded,
    then they are identical, the transport is a plain bearer chat endpoint
    with one config key, and the catalog opens with the GigaChat line."""
    copies = {}
    for path in SERVICE_PATHS:
        svc = _json(path)
        copies[path] = svc
        assert svc["base_url"] == ENDPOINT, path
        assert svc["auth_type"] == "bearer", path
        assert svc["config_key"] == "CLOUD_API_KEY", path
        assert svc.get("config_key2") is None, path
        assert svc["require_folder_id"] is False, path

        ids = [m["id"] for m in svc["models"]]
        assert ids[:4] == [
            "GigaChat/GigaChat-4-708B-A41",
            "ai-sage/GigaChat3.5-432B-A28B",
            "GigaChat/GigaChat-3-Pro",
            "ai-sage/GigaChat3-10B-A1.8B",
        ], path
        # The catalog spans the largest vendors on the same key.
        for prefix in ("openai/", "anthropic/", "google/", "deepseek-ai/"):
            assert any(mid.startswith(prefix) for mid in ids), (path, prefix)

    assert copies[SERVICE_PATHS[0]] == copies[SERVICE_PATHS[1]], \
        "service copies diverged"


def test_every_model_declares_an_output_limit():
    """Given the Cloud catalog, when a model is picked, then its entry
    declares the context window and the output limit the guards need."""
    for path in SERVICE_PATHS:
        for model in _json(path)["models"]:
            assert model.get("context_window"), (path, model)
            assert model.get("max_tokens"), (path, model)


# --- Scenario 2 - RAG recommendation ----------------------------------------


def test_rag_models_recommend_gigachat4_first():
    """Given the Cloud service definitions, when the RAG model list is read,
    then a GigaChat 4 model is recommended first and cheaper models follow."""
    for path in SERVICE_PATHS:
        rag = [m["id"] for m in _json(path)["rag_models"]]
        assert rag[0] == "GigaChat/GigaChat-4-708B-A41", path
        assert "openai/gpt-5.4-mini" in rag, path
        assert "google/gemini-2.5-flash" in rag, path


# --- Scenario 3 - sending a message -----------------------------------------


def test_sending_with_a_cloud_model_uses_the_bearer_endpoint():
    """Given the real Cloud service definition, when a message is sent with
    the GigaChat 4 model, then the request posts to the Cloud endpoint with
    a bearer header and the payload carries the selected model id."""
    from core.api_layer import send_request

    svc = _json("services/cloud.json")
    with patch("core.api_layer.get_services", return_value={"Cloud": svc}), \
         patch("core.api_layer.load_config", return_value=_cfg()), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post") as mock_post:
        mock_post.return_value = _ok_response()

        result = send_request("Привет", {
            "service": "Cloud", "model": "GigaChat/GigaChat-4-708B-A41",
            "temperature": 0.3, "text": "System prompt.", "id": "scenario",
        })

        assert result == "Ответ"
        assert mock_post.call_args[0][0] == ENDPOINT
        headers = mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer cloud-key"
        payload = mock_post.call_args[1]["json"]
        assert payload["model"] == "GigaChat/GigaChat-4-708B-A41"


def test_sending_without_a_key_reports_the_missing_credential():
    """Given no Cloud key, when a message is sent, then the failure names
    the credential instead of surfacing a provider HTTP error."""
    from core.api_errors import ApiKeyMissingError
    from core.api_layer import send_request

    svc = _json("services/cloud.json")
    with patch("core.api_layer.get_services", return_value={"Cloud": svc}), \
         patch("core.api_layer.load_config", return_value={}), \
         patch("core.api_layer.load_skill_files_context", return_value=""):
        try:
            send_request("Привет", {"service": "Cloud", "model": "openai/gpt-5.4",
                                    "temperature": 0.3, "text": "sys", "id": "s"})
        except ApiKeyMissingError as exc:
            assert "Cloud" in str(exc)
        else:
            raise AssertionError("a missing Cloud key must be reported")


# --- Scenario 4 - connection test -------------------------------------------


def test_connection_test_probes_the_chat_endpoint():
    """Given the real Cloud service definition, when the UI connection test
    runs, then it posts a one-token probe to the chat endpoint with the
    stored key and reports the HTTP status."""
    from core.api_layer import test_connection

    svc = _json("services/cloud.json")
    with patch("core.api_layer.get_services", return_value={"Cloud": svc}), \
         patch("core.api_layer.requests.post") as mock_post:
        resp = MagicMock()
        resp.status_code = 200
        mock_post.return_value = resp

        ok, msg = test_connection("Cloud", _cfg())

        assert ok is True
        assert "OK" in msg
        assert mock_post.call_args[0][0] == ENDPOINT
        headers = mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer cloud-key"


def test_connection_test_reports_a_rejected_key():
    """Given a rejected key, when the connection test runs, then the answer
    is a failure carrying the provider status."""
    from core.api_layer import test_connection

    svc = _json("services/cloud.json")
    with patch("core.api_layer.get_services", return_value={"Cloud": svc}), \
         patch("core.api_layer.requests.post") as mock_post:
        resp = MagicMock()
        resp.status_code = 401
        resp.text = "unauthorized"
        mock_post.return_value = resp

        ok, msg = test_connection("Cloud", _cfg())

        assert ok is False
        assert "401" in msg


# --- Scenario 5 - the retired provider leaves no trace ----------------------


def test_retired_provider_is_absent_from_catalog_and_transport():
    """Given the shipped catalog and the transport layer, when they are
    inspected, then the retired provider host appears nowhere: its traffic
    cannot be built any more."""
    for path in SERVICE_PATHS:
        raw = (ROOT / path).read_text(encoding="utf-8")
        assert LEGACY_HOST not in raw, path
    raw = (ROOT / "core" / "api_layer.py").read_text(encoding="utf-8")
    assert LEGACY_HOST not in raw
    assert not (ROOT / "services" / "gigachat.json").exists()
    assert not (ROOT / "certs" / "russian_trusted_root_ca.pem").exists()

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
