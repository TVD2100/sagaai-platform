# -*- coding: utf-8 -*-
"""tests/scenarios/test_gigachat_models_scenario.py - user-level scenario
tests for the GigaChat provider: migration to the api.giga.chat endpoint
and the GigaChat-3 model family.

Scenarios (given -> when -> then):

  Scenario 1 - GigaChat-3 models are offered by both service copies:
               the base_url points at https://api.giga.chat, the legacy
               gigachat.devices.sberbank.ru/api host is gone and the model
               list leads with GigaChat-3-Ultra/Pro/Lightning while the
               GigaChat-2 family stays available.
  Scenario 2 - RAG generation recommends GigaChat-3-Pro first.
  Scenario 3 - sending a message with a GigaChat-3 model posts to the new
               endpoint and carries the selected model id.
  Scenario 4 - the connection test derives the /models URL from the
               service base_url and reaches api.giga.chat/v1/models.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

ROOT = Path(__file__).resolve().parent.parent.parent

SERVICE_PATHS = ("services/gigachat.json", "defaults/services/gigachat.json")
NEW_ENDPOINT = "https://api.giga.chat/v1/chat/completions"
LEGACY_HOST = "gigachat.devices.sberbank.ru/api"


def _json(path: str) -> dict:
    """Load one repository JSON file relative to the project root."""
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _cfg() -> dict:
    """Minimal GigaChat runtime config matching the service config keys."""
    return {"GIGACHAT_API_KEY": "creds-base64", "GIGACHAT_SCOPE": "GIGACHAT_API_PERS"}


# --- Scenario 1 - GigaChat-3 models on the new endpoint ----------------------


def test_service_copies_offer_gigachat3_models_on_new_endpoint():
    """Given the GigaChat service definitions, when both copies are loaded,
    then they are identical, the base_url is api.giga.chat, the legacy host
    is absent and the model list leads with the three GigaChat-3 models."""
    copies = {}
    for path in SERVICE_PATHS:
        svc = _json(path)
        copies[path] = svc
        assert svc["base_url"] == NEW_ENDPOINT, path
        ids = [m["id"] for m in svc["models"]]
        assert ids[:3] == ["GigaChat-3-Ultra", "GigaChat-3-Pro", "GigaChat-3-Lightning"], path
        assert "GigaChat-2" in ids, path
        raw = (ROOT / path).read_text(encoding="utf-8")
        assert LEGACY_HOST not in raw, path

    assert copies[SERVICE_PATHS[0]] == copies[SERVICE_PATHS[1]], \
        "service copies diverged"


def test_api_layer_has_no_legacy_host():
    """Given the transport layer, when it is inspected, then the legacy
    GigaChat API host is not referenced anywhere in it."""
    raw = (ROOT / "core" / "api_layer.py").read_text(encoding="utf-8")
    assert LEGACY_HOST not in raw


# --- Scenario 2 - RAG recommendation ----------------------------------------


def test_rag_models_recommend_gigachat3_pro():
    """Given the GigaChat service definitions, when the RAG model list is
    read, then GigaChat-3-Pro is recommended first and the other GigaChat-3
    models are offered."""
    for path in SERVICE_PATHS:
        rag = [m["id"] for m in _json(path)["rag_models"]]
        assert rag[0] == "GigaChat-3-Pro", path
        assert "GigaChat-3-Ultra" in rag, path
        assert "GigaChat-3-Lightning" in rag, path


# --- Scenario 3 - sending with a GigaChat-3 model ----------------------------


def test_sending_with_gigachat3_model_uses_new_endpoint():
    """Given the real GigaChat service definition, when a message is sent
    with model GigaChat-3-Ultra, then the request posts to the new endpoint
    and the payload carries the selected model id."""
    from core.api_layer import send_request

    svc = _json("services/gigachat.json")
    with patch("core.api_layer.get_services", return_value={"GigaChat": svc}), \
         patch("core.api_layer.load_config", return_value=_cfg()), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer._gigachat_token", return_value="giga-token"), \
         patch("core.api_layer.requests.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"choices": [{"message": {"content": "Ответ"}}]}
        mock_session.post.return_value = mock_resp
        mock_session.headers = {}
        mock_session.verify = True
        mock_session_cls.return_value = mock_session

        result = send_request("Привет", {
            "service": "GigaChat", "model": "GigaChat-3-Ultra",
            "temperature": 0.3, "text": "System prompt.", "id": "scenario",
        })

        assert result == "Ответ"
        assert mock_session.post.call_args[0][0] == NEW_ENDPOINT
        payload = mock_session.post.call_args[1]["json"]
        assert payload["model"] == "GigaChat-3-Ultra"


# --- Scenario 4 - connection test --------------------------------------------


def test_connection_test_derives_models_url_from_base_url():
    """Given the real GigaChat service definition, when the UI connection
    test runs, then it GETs the /models URL derived from the service
    base_url on api.giga.chat."""
    from core.api_layer import test_connection

    svc = _json("services/gigachat.json")
    with patch("core.api_layer.get_services", return_value={"GigaChat": svc}), \
         patch("core.api_layer._gigachat_token", return_value="giga-token"), \
         patch("core.api_layer.requests.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_session.get.return_value = mock_resp
        mock_session.headers = {}
        mock_session.verify = True
        mock_session_cls.return_value = mock_session

        ok, msg = test_connection("GigaChat", _cfg())

        assert ok is True
        assert "OK" in msg
        assert mock_session.get.call_args[0][0] == "https://api.giga.chat/v1/models"
