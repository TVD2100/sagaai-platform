# -*- coding: utf-8 -*-
"""
tests.test_assistant_function_tools - unit tests for core.assistant_tools.

Covers:
- native function-tool detection in core.api_layer;
- function_call extraction from Responses output;
- the Yandex Responses tool loop (mock HTTP):
  * happy path: function_call -> rag_search -> function_call_output -> final text
  * iteration limit;
  * fallback to textual messages on provider 400;
  * access control for rag_search bases;
- payload shape (tools, function_call_output items).
"""

import json
from unittest.mock import MagicMock, patch

from core.assistant_tools import (
    execute_assistant_rag_search,
    run_yandex_responses_tool_loop,
)


_FUNCTION_TOOL = {
    "type": "function",
    "name": "rag_search",
    "description": "Search a knowledge base",
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "query": {"type": "string"},
            "top_k": {"type": "integer"},
        },
        "required": ["slug", "query"],
    },
}

_WEB_TOOL = {"type": "web_search"}


# ─── function-call extraction / payload helpers in api_layer ────────────────

def test_normalise_tools_converts_strings_and_passes_dicts():
    from core.api_layer import _normalise_tools

    normalised = _normalise_tools(["web_search", _FUNCTION_TOOL])
    assert normalised[0] == {"type": "web_search"}
    assert normalised[1] is _FUNCTION_TOOL


def test_has_native_function_tools():
    from core.api_layer import _has_native_function_tools
    assert _has_native_function_tools([_WEB_TOOL, _FUNCTION_TOOL])
    assert _has_native_function_tools([_FUNCTION_TOOL])
    assert not _has_native_function_tools(["web_search"])
    assert not _has_native_function_tools([])


def test_extract_function_calls():
    from core.assistant_tools import _extract_function_calls
    data = {"output": [
        {"type": "function_call", "name": "rag_search", "call_id": "call_1",
         "arguments": '{"slug": "b1", "query": "docs"}'},
        {"type": "reasoning", "summary": [{"text": "hidden"}]},
        {"type": "function_call", "name": "rag_search", "call_id": "call_2",
         "arguments": "not-json"},
    ]}
    calls = _extract_function_calls(data)
    assert len(calls) == 2
    assert calls[0] == {"call_id": "call_1", "name": "rag_search",
                        "arguments": {"slug": "b1", "query": "docs"}}
    assert calls[1]["arguments"] == {}


# ─── rag_search execution / access control ──────────────────────────────────

def test_execute_rag_search_missing_args():
    out = execute_assistant_rag_search({})
    data = json.loads(out)
    assert data["ok"] is False
    assert "slug" in data["error"].lower() or "query" in data["error"].lower()


def test_execute_rag_search_access_denied():
    assistant = {"slug": "docs_bot"}
    with patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"allowed_base"}):
        out = execute_assistant_rag_search(
            {"slug": "other_base", "query": "q"}, assistant=assistant
        )
    data = json.loads(out)
    assert data["ok"] is False
    assert "access denied" in data["error"].lower()


def test_execute_rag_search_denied_when_no_bases_assigned():
    """An assistant without RAG bases in its manifest is denied ALL rag_search."""
    with patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value=set()):
        out = execute_assistant_rag_search(
            {"slug": "b1", "query": "docs"}, assistant={"slug": "docs_bot"}
        )
    data = json.loads(out)
    assert data["ok"] is False
    assert "access denied" in data["error"].lower()


def test_execute_rag_search_ok():
    fake_hits = [
        {"source": "docs/a.md", "chunk_index": 0, "score": 0.9,
         "text": "Фрагмент документации"},
    ]
    with patch("core.assistant_tools.search_base", return_value=fake_hits), \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}):
        out = execute_assistant_rag_search(
            {"slug": "b1", "query": "docs", "top_k": 3}
        )
    data = json.loads(out)
    assert data["ok"] is True
    assert data["count"] == 1
    assert "Фрагмент документации" in data["text"]


# ─── loop happy path ────────────────────────────────────────────────────────

def _resp(*output_items, usage_tokens=(10, 20)):
    return {
        "output": list(output_items),
        "usage": {"input_tokens": usage_tokens[0],
                  "output_tokens": usage_tokens[1]},
    }


def test_execute_rag_search_search_base_error():
    """A search_base failure is returned as a JSON error, not raised."""
    with patch("core.assistant_tools.search_base",
               side_effect=RuntimeError("index down")), \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}):
        out = execute_assistant_rag_search(
            {"slug": "b1", "query": "docs"}, assistant={"slug": "docs_bot"}
        )
    data = json.loads(out)
    assert data["ok"] is False
    assert "index down" in data["error"]


def test_loop_single_rag_call_happy_path():
    first = _resp(
        {"type": "function_call", "name": "rag_search", "call_id": "c1",
         "arguments": '{"slug": "b1", "query": "опенсорс"}'},
    )
    final = _resp({
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "Итоговый ответ"}],
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = [first, final]

    with patch("core.assistant_tools.search_base",
               return_value=[{"source": "a", "chunk_index": 0, "score": 1.0,
                              "text": "чанк"}]) as search_mock, \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}):
        with patch("core.assistant_tools.requests.post",
                   return_value=mock_resp) as post:
            tool_events = []
            result = run_yandex_responses_tool_loop(
                "https://ai.api.cloud.yandex.net/v1", "k", "f", "m",
                "sys", [], "вопрос", temperature=0.3,
                tools_list=[_FUNCTION_TOOL],
                cfg={}, svc_name="YandexAI",
                assistant={"slug": "docs_bot"},
                on_tool_call=lambda e: tool_events.append(e),
            )

    assert result == "Итоговый ответ"
    assert len(mock_resp.json.call_args_list) == 2
    assert search_mock.call_count == 1
    assert search_mock.call_args[0][0] == "b1"
    assert len(tool_events) == 1
    assert tool_events[0]["name"] == "rag_search"

    # Check the second request carried function_call + function_call_output.
    last_payload = post.call_args[1]["json"]
    types = [i.get("type") for i in last_payload["input"] if isinstance(i, dict)]
    assert "function_call" in types
    assert "function_call_output" in types
    assert last_payload["tools"][0]["type"] == "function"


def test_loop_no_tool_call_single_request():
    resp = _resp({
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "Без инструментов"}],
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = resp
    with patch("core.assistant_tools.requests.post", return_value=mock_resp):
        result = run_yandex_responses_tool_loop(
            "https://ai.api.cloud.yandex.net/v1", "k", "f", "m",
            "sys", [], "q", temperature=0.3,
            tools_list=[_WEB_TOOL, _FUNCTION_TOOL],
            cfg={}, svc_name="YandexAI",
        )
    assert result == "Без инструментов"


# ─── iteration limit / fallback ─────────────────────────────────────────────

def test_loop_iteration_limit():
    """The loop stops after max_tool_calls tool-requests (+1 final)."""
    first = _resp({
        "type": "function_call", "name": "rag_search", "call_id": "c1",
        "arguments": '{"slug": "b1", "query": "q"}',
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = first
    with patch("core.assistant_tools.search_base",
               return_value=[]) as search_mock, \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}):
        with patch("core.assistant_tools.requests.post",
                   return_value=mock_resp) as post:
            result = run_yandex_responses_tool_loop(
                "https://ai.api.cloud.yandex.net/v1", "k", "f", "m",
                "sys", [], "q", temperature=0.3,
                tools_list=[_FUNCTION_TOOL], max_tool_calls=2,
                cfg={}, svc_name="YandexAI",
            )
    # 2 tool requests + 1 final = 3 HTTP calls
    assert post.call_count == 3
    assert search_mock.call_count == 3
    assert result == ""


def test_loop_400_fallback_textual():
    """A provider 400 on native function items triggers a textual retry."""
    first = _resp({
        "type": "function_call", "name": "rag_search", "call_id": "c1",
        "arguments": '{"slug": "b1", "query": "q"}',
    })
    final = _resp({
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "После fallback"}],
    })

    def _side_effect(*a, **kw):
        # First native request OK, second (with native items) -> 400,
        # third (textual) OK.
        idx = _side_effect.calls
        _side_effect.calls += 1
        if idx == 1:
            r = MagicMock(); r.status_code = 400; r.text = '{"error": {"message": "bad"}}'
            return r
        r = MagicMock(); r.status_code = 200
        r.json.return_value = first if idx == 0 else final
        return r

    _side_effect.calls = 0
    with patch("core.assistant_tools.search_base",
               return_value=[{"source": "a", "chunk_index": 0, "score": 1.0,
                              "text": "чанк"}]), \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}):
        with patch("core.assistant_tools.requests.post",
                   side_effect=_side_effect) as post:
            result = run_yandex_responses_tool_loop(
                "https://ai.api.cloud.yandex.net/v1", "k", "f", "m",
                "sys", [], "q", temperature=0.3,
                tools_list=[_FUNCTION_TOOL],
                cfg={}, svc_name="YandexAI",
            )
    assert result == "После fallback"
    # The retry payload must contain NO native function items.
    retry_payload = post.call_args_list[2][1]["json"]
    for item in retry_payload["input"]:
        if isinstance(item, dict):
            assert item.get("type") not in ("function_call", "function_call_output")


# ─── send_request routing tests ─────────────────────────────────────────────

def test_send_request_routes_to_tool_loop_when_native_function_tools(monkeypatch):
    """A Yandex assistant with a native function tool uses the new loop
    and does NOT force a web_search tool_choice."""
    from core import api_layer

    cfg = {"YANDEX_API_KEY": "iam-token", "YANDEX_FOLDER_ID": "folder-id"}
    svc = {
        "name": "YandexAI",
        "auth_type": "yandex_iam",
        "base_url": "https://ai.api.cloud.yandex.net/v1",
        "config_key": "YANDEX_API_KEY",
        "config_key2": "YANDEX_FOLDER_ID",
        "models": [{"id": "m"}],
    }
    assistant = {
        "service": "YandexAI",
        "model": "m",
        "temperature": 0.3,
        "text": "sys",
        "tools": ["web_search", _FUNCTION_TOOL],
        "slug": "docs_bot",
    }

    def _fake_loop(base_url, api_key, folder_id, model, sys_text, hist_msgs,
                   user_content, **kwargs):
        kwargs["user_content"] = user_content
        _fake_loop.calls.append(kwargs)
        return "loop-answer"
    _fake_loop.calls = []

    monkeypatch.setattr(api_layer, "load_config", lambda: cfg)
    monkeypatch.setattr(api_layer, "get_services",
                        lambda: {"YandexAI": svc})
    monkeypatch.setattr(api_layer, "load_assistant_files_context", lambda *a: "")
    monkeypatch.setattr(api_layer, "_assistant_rag_context", lambda *a: "ctxt")
    import core.assistant_tools as at
    monkeypatch.setattr(at, "run_yandex_responses_tool_loop",
                        _fake_loop)

    result = api_layer.send_request("вопрос", assistant)
    assert result == "loop-answer"
    assert len(_fake_loop.calls) == 1
    kwargs = _fake_loop.calls[0]
    assert kwargs["tool_choice"] is None
    assert kwargs["assistant"] is assistant
    # Auto-RAG context must be skipped for native function tools.
    assert "База знаний" not in kwargs["user_content"]


def test_send_request_preserves_legacy_yandex_path(monkeypatch):
    """No native function tools -> the old single-request path stays intact."""
    from core import api_layer

    cfg = {"YANDEX_API_KEY": "iam-token", "YANDEX_FOLDER_ID": "folder-id"}
    svc = {
        "name": "YandexAI",
        "auth_type": "yandex_iam",
        "base_url": "https://ai.api.cloud.yandex.net/v1",
        "config_key": "YANDEX_API_KEY",
        "config_key2": "YANDEX_FOLDER_ID",
        "models": [{"id": "m"}],
    }
    assistant = {
        "service": "YandexAI", "model": "m", "temperature": 0.3,
        "text": "sys", "tools": ["web_search"],
    }
    monkeypatch.setattr(api_layer, "load_config", lambda: cfg)
    monkeypatch.setattr(api_layer, "get_services",
                        lambda: {"YandexAI": svc})
    monkeypatch.setattr(api_layer, "load_assistant_files_context", lambda *a: "")
    monkeypatch.setattr(api_layer, "_assistant_rag_context", lambda *a: "ctxt")

    final = _resp({"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "legacy"}]})
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = final
    with patch("core.api_layer.requests.post", return_value=mock_resp) as post:
        result = api_layer.send_request("вопрос", assistant)

    assert result == "legacy"
    payload = post.call_args[1]["json"]
    assert payload["tool_choice"] == {"type": "web_search"}
    # Auto-RAG context is attached to legacy path.
    assert "База знаний" in payload["input"][-1]["content"]


# ─── per-assistant web-search overrides ────────────────────────────────────

def test_yandex_web_search_config_parses_provider_values():
    """Provider context size is validated (invalid -> medium) and a
    comma/space-separated domain string is split and deduped."""
    from core.api_layer import _yandex_web_search_config

    ctx, doms = _yandex_web_search_config(
        {
            "YandexAI_web_search_context_size": "bogus",
            "YandexAI_web_search_allowed_domains": (
                "docs.example.org, docs.example.org api.example.com"
            ),
        },
        "YandexAI",
    )
    assert ctx == "medium"
    assert doms == ["docs.example.org", "api.example.com"]


def test_yandex_web_search_config_list_values_and_defaults():
    """Legacy array values are accepted, and empty config gives defaults."""
    from core.api_layer import _yandex_web_search_config

    ctx, doms = _yandex_web_search_config(
        {
            "YandexAI_web_search_context_size": "high",
            "YandexAI_web_search_allowed_domains": ["a.com", "a.com", "", "b.org"],
        },
        "YandexAI",
    )
    assert ctx == "high"
    assert doms == ["a.com", "b.org"]

    ctx2, doms2 = _yandex_web_search_config({}, "YandexAI")
    assert ctx2 == "medium"
    assert doms2 == []


def test_assistant_web_search_config_prefers_manifest_overrides(monkeypatch):
    """Per-assistant manifest settings win over provider-level config."""
    from core.api_layer import _assistant_web_search_config

    monkeypatch.setattr(
        "core.api_layer._yandex_web_search_config",
        lambda cfg, svc: ("medium", ["api.global.example"]),
    )
    monkeypatch.setattr(
        "core.assistant_folders.get_assistant_web_search_settings",
        lambda slug: {"context_size": "high",
                      "allowed_domains": ["docs.local.example", "docs.local.example"]},
    )
    ctx, doms = _assistant_web_search_config(
        {"slug": "docs_bot"},
        {"YandexAI_web_search_context_size": "low"},
        "YandexAI",
    )
    assert ctx == "high"
    # Duplicates are removed.
    assert doms == ["docs.local.example"]


def test_assistant_web_search_config_falls_back_to_provider(monkeypatch):
    """Without manifest overrides the provider-level config is used."""
    from core.api_layer import _assistant_web_search_config

    monkeypatch.setattr(
        "core.api_layer._yandex_web_search_config",
        lambda cfg, svc: ("low", ["api.global.example"]),
    )
    monkeypatch.setattr(
        "core.assistant_folders.get_assistant_web_search_settings",
        lambda slug: {},
    )
    ctx, doms = _assistant_web_search_config(
        {"slug": "docs_bot"}, {}, "YandexAI"
    )
    assert ctx == "low"
    assert doms == ["api.global.example"]


def test_loop_assistant_no_web_search_tool_has_no_web_tool():
    """Per-assistant web_search tool still goes through, but manifest
    overrides are only applied when a web_search tool exists."""
    first = _resp(
        {"type": "function_call", "name": "rag_search", "call_id": "c1",
         "arguments": '{"slug": "b1", "query": "q"}'},
    )
    final = _resp({
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "ITOG"}],
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = [first, final]

    with patch("core.assistant_tools.search_base",
               return_value=[{"source": "a", "chunk_index": 0, "score": 1.0,
                              "text": "чанк"}]), \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}), \
         patch("core.assistant_tools._yandex_web_search_config",
               return_value=("high", ["docs.override.example"])):
        with patch("core.assistant_tools.requests.post",
                   return_value=mock_resp) as post:
            run_yandex_responses_tool_loop(
                "https://ai.api.cloud.yandex.net/v1", "k", "f", "m",
                "sys", [], "q", temperature=0.3,
                tools_list=[_FUNCTION_TOOL],
                cfg={}, svc_name="YandexAI",
                assistant={"slug": "docs_bot"},
            )

    last_payload = post.call_args_list[1][1]["json"]
    web_tools = [t for t in last_payload["tools"]
                 if t.get("type") == "web_search"]
    assert web_tools == []


def test_loop_unknown_function_tool_returns_error():
    """Unknown function calls produce an error output, not a crash."""
    first = _resp(
        {"type": "function_call", "name": "delete_db", "call_id": "c1",
         "arguments": '{}'},
    )
    final = _resp({
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "Done"}],
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = [first, final]

    with patch("core.assistant_tools.requests.post",
               return_value=mock_resp) as post:
        result = run_yandex_responses_tool_loop(
            "https://ai.api.cloud.yandex.net/v1", "k", "f", "m",
            "sys", [], "q", temperature=0.3,
            tools_list=[_FUNCTION_TOOL],
            cfg={}, svc_name="YandexAI",
        )

    assert result == "Done"
    second_payload = post.call_args_list[1][1]["json"]
    outputs = [i for i in second_payload["input"]
               if isinstance(i, dict) and i.get("type") == "function_call_output"]
    assert outputs
    data = json.loads(outputs[0]["output"])
    assert 'Unknown function tool' in data.get('error', '')


def test_loop_payload_uses_assistant_web_search_overrides():
    """The tool-loop payload applies per-assistant web-search config."""
    first = _resp(
        {"type": "function_call", "name": "rag_search", "call_id": "c1",
         "arguments": '{"slug": "b1", "query": "q"}'},
    )
    final = _resp({
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "ITOG"}],
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = [first, final]

    with patch("core.assistant_tools.search_base",
               return_value=[{"source": "a", "chunk_index": 0, "score": 1.0,
                              "text": "чанк"}]), \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}), \
         patch("core.assistant_tools._yandex_web_search_config",
               return_value=("high", ["docs.override.example"])):
        with patch("core.assistant_tools.requests.post",
                   return_value=mock_resp) as post:
            run_yandex_responses_tool_loop(
                "https://ai.api.cloud.yandex.net/v1", "k", "f", "m",
                "sys", [], "q", temperature=0.3,
                tools_list=[_WEB_TOOL, _FUNCTION_TOOL],
                cfg={}, svc_name="YandexAI",
                assistant={"slug": "docs_bot"},
            )

    last_payload = post.call_args_list[1][1]["json"]
    web_tool = next(t for t in last_payload["tools"]
                    if t.get("type") == "web_search")
    assert web_tool.get("search_context_size") == "high"
    assert web_tool.get("filters", {}).get("allowed_domains") == [
        "docs.override.example"
    ]
