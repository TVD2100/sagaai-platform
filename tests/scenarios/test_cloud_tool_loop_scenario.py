# -*- coding: utf-8 -*-
"""tests/scenarios/test_cloud_tool_loop_scenario.py - user-level scenarios for
an assistant that calls function tools on the Cloud provider.

Cloud is an OpenAI-compatible gateway speaking chat/completions, so the
tool loop posts twice: the model asks for a tool, the platform executes it
locally, the result rides back as a ``role: tool`` message and the final
answer is returned to the caller.

Scenarios (given -> when -> then), driven through the public entry point
``core.api_layer.send_request`` on a mocked HTTP transport:

  Scenario 1 - happy path: the assistant searches its knowledge base, the
               wire payload carries the nested OpenAI tool shape and the
               catalog-declared ``extra_payload``, and the retrieved chunk
               reaches the final answer.
  Scenario 2 - access control: the model asks for a base that is not bound
               to the assistant, the tool refuses, and the refusal is fed
               back so the model still answers.
  Scenario 3 - degraded gateway: the endpoint rejects the tools payload,
               the loop retries once without tools and the assistant
               answers plainly instead of failing.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CLOUD_ENDPOINT = "https://foundation-models.api.cloud.ru/v1/chat/completions"
CLOUD_MODEL = "GigaChat/GigaChat-4-708B-A41"
ASSISTANT_PROMPT = "You answer from the knowledge base."

# The flat Responses-style declaration the platform stores for an assistant.
RAG_TOOL = {
    "type": "function",
    "name": "rag_search",
    "description": "Search a knowledge base",
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "query": {"type": "string"},
        },
        "required": ["slug", "query"],
    },
}


def _cloud_service() -> dict:
    """The real shipped Cloud service definition."""
    return json.loads((ROOT / "services" / "cloud.json").read_text(encoding="utf-8"))


def _assistant(**overrides) -> dict:
    """A saved assistant bound to the Cloud provider and one RAG base."""
    card = {
        "id": "docs_bot",
        "slug": "docs_bot",
        "service": "Cloud",
        "model": CLOUD_MODEL,
        "temperature": 0.3,
        "text": ASSISTANT_PROMPT,
        "tools": [dict(RAG_TOOL)],
        "max_tool_calls": 3,
    }
    card.update(overrides)
    return card


def _tool_call_response(call_id: str = "call_1", query: str = "release notes") -> dict:
    """One chat/completions body where the model asks for rag_search."""
    return {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "rag_search",
                        "arguments": json.dumps({"slug": "docs", "query": query}),
                    },
                }],
            },
        }],
        "usage": {"prompt_tokens": 40, "completion_tokens": 8},
    }


def _answer_response(text: str) -> dict:
    """One chat/completions body with a plain assistant answer."""
    return {
        "choices": [{"finish_reason": "stop",
                       "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 60, "completion_tokens": 12},
    }


def _ok(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = body
    return resp


def _rejected(message: str) -> MagicMock:
    resp = MagicMock()
    resp.ok = False
    resp.status_code = 400
    resp.json.return_value = {"error": {"message": message}}
    resp.text = message
    return resp


class Wire:
    """Mocked chat/completions endpoint that snapshots every request body.

    The loop keeps one growing ``messages`` list and hands it to the payload
    builder, so a recorded request body must be copied at send time -
    otherwise later rounds rewrite what an earlier round appeared to send.
    """

    def __init__(self):
        self.responses = []
        self.requests = []

    def post(self, url, headers=None, json=None, **kwargs):  # noqa: A002
        self.requests.append({
            "url": url,
            "headers": dict(headers or {}),
            "json": copy.deepcopy(json),
        })
        if not self.responses:
            raise AssertionError("the code sent an unexpected extra request")
        return self.responses.pop(0)

    @property
    def bodies(self) -> list:
        return [r["json"] for r in self.requests]


@pytest.fixture()
def cloud(monkeypatch, tmp_path):
    """Route every Cloud request through one mocked endpoint."""
    import os

    os.environ["SAGAAI_DATA_DIR"] = str(tmp_path / "data")
    import core.api_layer as api_layer

    monkeypatch.setattr(api_layer, "get_services",
                        lambda: {"Cloud": _cloud_service()})
    monkeypatch.setattr(api_layer, "load_config",
                        lambda: {"CLOUD_API_KEY": "cloud-key"})
    monkeypatch.setattr(api_layer, "load_assistant_files_context",
                        lambda *a, **k: "")
    wire = Wire()
    monkeypatch.setattr(api_layer.requests, "post", wire.post)
    return wire


# --- Scenario 1 - happy path -------------------------------------------------


def test_scenario_assistant_calls_a_tool_on_cloud(cloud):
    """Given an assistant on Cloud with a native rag_search tool,
    when  the user asks a question and the model requests a search,
    then  the tool runs locally, its result is fed back as a tool message,
          the wire payload uses the nested OpenAI shape plus the catalog
          extra_payload, and the final answer names the retrieved fact."""
    from core.api_layer import send_request

    cloud.responses = [
        _ok(_tool_call_response()),
        _ok(_answer_response("Release 3.2.0 replaced GigaChat with Cloud.")),
    ]

    with pytest.MonkeyPatch.context() as mp:
        import core.assistant_tools as at
        mp.setattr(at, "search_base", lambda *a, **k: [
            {"chunk_id": 7, "source": "docs/release.md", "chunk_index": 2,
             "score": 0.91, "text": "Release 3.2.0 replaced GigaChat with Cloud."},
        ])
        mp.setattr(at, "_assistant_allowed_rag_bases", lambda assistant: {"docs"})
        tool_events = []
        answer = send_request(
            "What replaced GigaChat?", assistant=_assistant(),
            on_tool_call=lambda e: tool_events.append(e),
        )

    assert answer == "Release 3.2.0 replaced GigaChat with Cloud."
    assert len(cloud.requests) == 2
    assert tool_events and tool_events[0]["name"] == "rag_search"

    first_req = cloud.requests[0]
    first = first_req["json"]
    assert first_req["url"] == CLOUD_ENDPOINT
    assert first_req["headers"]["Authorization"] == "Bearer cloud-key"
    # Native tool arrives nested - a flat Responses entry would be ignored.
    sent_tool = first["tools"][0]
    assert sent_tool["function"]["name"] == "rag_search"
    assert "name" not in sent_tool
    # The catalog declares enable_thinking for every Cloud model.
    assert first["chat_template_kwargs"] == {"enable_thinking": True}
    assert first["messages"][0]["role"] == "system"
    assert first["messages"][0]["content"].startswith(ASSISTANT_PROMPT)
    assert first["messages"][-1]["content"] == "What replaced GigaChat?"

    second = cloud.bodies[1]
    roles = [m["role"] for m in second["messages"]]
    assert roles[-2:] == ["assistant", "tool"]
    tool_msg = second["messages"][-1]
    assert tool_msg["tool_call_id"] == "call_1"
    result = json.loads(tool_msg["content"])
    assert result["ok"] is True
    assert result["hits"][0]["chunk_id"] == 7


# --- Scenario 2 - access control ---------------------------------------------


def test_scenario_unassigned_base_is_refused_and_the_answer_still_lands(cloud):
    """Given the model asks for a knowledge base that is not bound to the
          assistant,
    when  the tool executes,
    then  the refusal travels back as the tool result, the request continues
          and the assistant still produces an answer."""
    from core.api_layer import send_request

    cloud.responses = [
        _ok(_tool_call_response(query="secret plans")),
        _ok(_answer_response("I have no access to that base.")),
    ]

    with pytest.MonkeyPatch.context() as mp:
        import core.assistant_tools as at
        mp.setattr(at, "_assistant_allowed_rag_bases", lambda assistant: set())
        answer = send_request("Tell me the secret plans", assistant=_assistant())

    assert answer == "I have no access to that base."
    tool_msg = cloud.bodies[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    refusal = json.loads(tool_msg["content"])
    assert refusal["ok"] is False
    assert "Access denied" in refusal["error"]


# --- Scenario 3 - degraded gateway -------------------------------------------


def test_scenario_gateway_without_tool_support_answers_plainly(cloud):
    """Given the endpoint rejects the tools payload with HTTP 400,
    when  the assistant sends its question,
    then  the loop retries once without tools and the user still gets a
          plain answer instead of an error."""
    from core.api_layer import send_request

    cloud.responses = [
        _rejected("tools are not supported by this model"),
        _ok(_answer_response("Plain answer without tools.")),
    ]

    answer = send_request("What replaced GigaChat?", assistant=_assistant())

    assert answer == "Plain answer without tools."
    assert len(cloud.requests) == 2
    retried = cloud.bodies[1]
    assert "tools" not in retried
    assert retried["messages"][-1]["content"] == "What replaced GigaChat?"


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
