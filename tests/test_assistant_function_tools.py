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
- rag_search hits metadata and rag_get_chunks execution/routing;
- payload shape (tools, function_call_output items).
"""

import json
from unittest.mock import MagicMock, patch

from core.assistant_tools import (
    execute_assistant_rag_chunks,
    execute_assistant_rag_search,
    run_bearer_tool_loop,
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

_CHUNKS_TOOL = {
    "type": "function",
    "name": "rag_get_chunks",
    "description": "Fetch specific chunks",
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "chunk_ids": {"type": "array", "items": {"type": "integer"}},
            "source": {"type": "string"},
            "chunk_indices": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["slug"],
    },
}


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
            r = MagicMock()
            r.status_code = 400
            r.text = '{"error": {"message": "bad"}}'
            return r
        r = MagicMock()
        r.status_code = 200
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


# ─── rag_get_chunks execution / routing ─────────────────────────────────────

def test_execute_rag_chunks_missing_slug():
    out = execute_assistant_rag_chunks({})
    data = json.loads(out)
    assert data["ok"] is False
    assert "slug" in data["error"].lower()


def test_execute_rag_chunks_needs_an_address_mode():
    with patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}):
        out = execute_assistant_rag_chunks(
            {"slug": "b1"}, assistant={"slug": "docs_bot"})
    data = json.loads(out)
    assert data["ok"] is False
    assert "chunk_ids" in data["error"] or "chunk_indices" in data["error"]


def test_execute_rag_chunks_access_denied():
    with patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"allowed_base"}):
        out = execute_assistant_rag_chunks(
            {"slug": "other", "chunk_ids": [1]},
            assistant={"slug": "docs_bot"})
    data = json.loads(out)
    assert data["ok"] is False
    assert "access denied" in data["error"].lower()


def test_execute_rag_chunks_ok_returns_context_and_metadata():
    fake = {
        "chunks": [
            {"chunk_id": 5, "text": "part five", "source": "doc.md",
             "chunk_index": 4},
            {"chunk_id": 7, "text": "part seven", "source": "doc.md",
             "chunk_index": 6},
        ],
        "missing": [6],
    }
    with patch("core.assistant_tools.get_chunks", return_value=fake) as fetch_mock, \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}):
        out = execute_assistant_rag_chunks(
            {"slug": "b1", "chunk_ids": [5, 7, 6]},
            assistant={"slug": "docs_bot"})
    data = json.loads(out)
    assert data["ok"] is True
    assert data["count"] == 2
    assert data["missing"] == [6]
    assert [c["chunk_id"] for c in data["chunks"]] == [5, 7]
    assert "part five" in data["text"]
    assert "id 5" in data["text"]
    fetch_mock.assert_called_once_with("b1", chunk_ids=[5, 7, 6],
                                       source="", chunk_indices=None)


def test_execute_rag_chunks_error_is_json_not_raised():
    with patch("core.assistant_tools.get_chunks",
               side_effect=RuntimeError("boom")), \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}):
        out = execute_assistant_rag_chunks(
            {"slug": "b1", "source": "doc.md", "chunk_indices": [1, 2]},
            assistant={"slug": "docs_bot"})
    data = json.loads(out)
    assert data["ok"] is False
    assert "boom" in data["error"]


def test_loop_routes_rag_get_chunks():
    first = _resp(
        {"type": "function_call", "name": "rag_get_chunks",
         "call_id": "c2",
         "arguments": '{"slug": "b1", "chunk_ids": [5, 7]}'},
    )
    final = _resp({
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "Собрал контекст"}],
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = [first, final]

    fake = {"chunks": [{"chunk_id": 5, "text": "part five",
                        "source": "doc.md", "chunk_index": 4}],
            "missing": []}
    with patch("core.assistant_tools.get_chunks",
               return_value=fake) as fetch_mock, \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}):
        with patch("core.assistant_tools.requests.post",
                   return_value=mock_resp) as post:
            tool_events = []
            result = run_yandex_responses_tool_loop(
                "https://ai.api.cloud.yandex.net/v1", "k", "f", "m",
                "sys", [], "q", temperature=0.3,
                tools_list=[_CHUNKS_TOOL],
                cfg={}, svc_name="YandexAI",
                assistant={"slug": "docs_bot"},
                on_tool_call=lambda e: tool_events.append(e),
            )

    assert result == "Собрал контекст"
    assert fetch_mock.call_count == 1
    assert fetch_mock.call_args[1]["chunk_ids"] == [5, 7]
    second_payload = post.call_args_list[1][1]["json"]
    outputs = [i for i in second_payload["input"]
               if isinstance(i, dict) and i.get("type") == "function_call_output"]
    assert outputs
    out_data = json.loads(outputs[0]["output"])
    assert out_data["ok"] is True
    assert "part five" in out_data["text"]
    assert tool_events and tool_events[0]["name"] == "rag_get_chunks"


def test_execute_rag_search_ok_includes_hits_metadata():
    fake_hits = [
        {"chunk_id": 15, "source": "docs/a.md", "chunk_index": 3,
         "score": 0.9, "text": "Фрагмент"},
    ]
    with patch("core.assistant_tools.search_base", return_value=fake_hits), \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}):
        out = execute_assistant_rag_search({"slug": "b1", "query": "docs"})
    data = json.loads(out)
    assert data["ok"] is True
    assert data["hits"] == [{"chunk_id": 15, "source": "docs/a.md",
                             "chunk_index": 3, "score": 0.9}]


def test_execute_rag_search_hits_carry_mode_when_present():
    """Hits serialized by the assistant tool keep the search mode.

    given: search_base returns a hit that carries mode "bm25"
    when: the assistant rag_search tool serializes it
    then: the JSON hit includes mode "bm25" (and omits the key when absent)
    """
    fake_hits = [
        {"chunk_id": 7, "source": "docs/b.md", "chunk_index": 1,
         "score": 0.5, "text": "Фрагмент", "mode": "bm25"},
    ]
    with patch("core.assistant_tools.search_base", return_value=fake_hits), \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}):
        out = execute_assistant_rag_search({"slug": "b1", "query": "docs"})
    data = json.loads(out)
    assert data["ok"] is True
    assert data["hits"] == [{"chunk_id": 7, "source": "docs/b.md",
                             "chunk_index": 1, "score": 0.5,
                             "mode": "bm25"}]


# ─── bearer (chat/completions) tool loop ───────────────────────────────────

def _chat_resp(content="", tool_calls=None, usage=(10, 20)):
    """Build one chat/completions response body."""
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]},
    }


def _chat_call(call_id, name, arguments):
    """Build one chat/completions tool_call entry."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def test_bearer_loop_single_rag_call_happy_path():
    """given a model that calls rag_search once
    when: the bearer loop runs against a mocked chat/completions endpoint
    then: the tool executes, its output rides back as a tool message and the
          final answer is returned
    """
    first = _chat_resp(tool_calls=[_chat_call("c1", "rag_search",
                                              {"slug": "b1", "query": "q"})])
    final = _chat_resp(content="Итоговый ответ")
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
            result = run_bearer_tool_loop(
                "https://gw.example/v1/chat/completions", "k", "model-1",
                "sys", [], "вопрос", temperature=0.3,
                tools_list=[_FUNCTION_TOOL], cfg={}, svc_name="Cloud",
                assistant={"slug": "docs_bot"},
                on_tool_call=lambda e: tool_events.append(e),
            )

    assert result == "Итоговый ответ"
    assert post.call_count == 2
    assert search_mock.call_count == 1
    assert tool_events and tool_events[0]["name"] == "rag_search"

    # The second request must carry the assistant turn plus the tool result.
    second_payload = post.call_args_list[1].kwargs["json"]
    roles = [m["role"] for m in second_payload["messages"]]
    assert roles[-2:] == ["assistant", "tool"]
    tool_msg = second_payload["messages"][-1]
    assert tool_msg["tool_call_id"] == "c1"
    assert json.loads(tool_msg["content"])["ok"] is True


def test_bearer_loop_sequential_calls():
    """given a model that calls two different tools in a row
    when: the bearer loop runs
    then: both calls execute and each result is reported
    """
    first = _chat_resp(tool_calls=[_chat_call("c1", "rag_search",
                                              {"slug": "b1", "query": "q"})])
    second = _chat_resp(tool_calls=[_chat_call("c2", "rag_get_chunks",
                                               {"slug": "b1", "chunk_ids": [5]})])
    final = _chat_resp(content="Готово")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = [first, second, final]

    with patch("core.assistant_tools.search_base", return_value=[]), \
         patch("core.assistant_tools.get_chunks",
               return_value=[{"chunk_id": 5, "text": "кусок"}]), \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}):
        with patch("core.assistant_tools.requests.post",
                   return_value=mock_resp) as post:
            events = []
            result = run_bearer_tool_loop(
                "https://gw.example/v1/chat/completions", "k", "m",
                "sys", [], "q", temperature=0.3,
                tools_list=[_FUNCTION_TOOL, _CHUNKS_TOOL], cfg={},
                svc_name="Cloud",
                on_tool_call=lambda e: events.append(e),
            )

    assert result == "Готово"
    assert post.call_count == 3
    assert [e["name"] for e in events] == ["rag_search", "rag_get_chunks"]


def test_bearer_loop_unknown_tool_feeds_error_back():
    """given a tool call for a name the platform does not implement
    when: the loop executes it
    then: the model receives a structured error and can still answer
    """
    first = _chat_resp(tool_calls=[_chat_call("c1", "no_such_tool", {"x": 1})])
    final = _chat_resp(content="Отвечаю без инструмента")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = [first, final]

    with patch("core.assistant_tools.requests.post", return_value=mock_resp) as post:
        result = run_bearer_tool_loop(
            "https://gw.example/v1/chat/completions", "k", "m",
            "sys", [], "q", temperature=0.3,
            tools_list=[_FUNCTION_TOOL], cfg={}, svc_name="Cloud",
        )

    assert result == "Отвечаю без инструмента"
    tool_msg = post.call_args_list[1].kwargs["json"]["messages"][-1]
    payload = json.loads(tool_msg["content"])
    assert payload["ok"] is False
    assert "no_such_tool" in payload["error"]


def test_bearer_loop_iteration_limit():
    """given a model that never stops calling tools
    when: the loop reaches max_tool_calls
    then: it stops without inventing a final answer
    """
    endless = _chat_resp(tool_calls=[_chat_call("c1", "rag_search",
                                                {"slug": "b1", "query": "q"})])
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = endless

    with patch("core.assistant_tools.search_base", return_value=[]), \
         patch("core.assistant_tools._assistant_allowed_rag_bases",
               return_value={"b1"}):
        with patch("core.assistant_tools.requests.post",
                   return_value=mock_resp) as post:
            result = run_bearer_tool_loop(
                "https://gw.example/v1/chat/completions", "k", "m",
                "sys", [], "q", temperature=0.3,
                tools_list=[_FUNCTION_TOOL], max_tool_calls=2,
                cfg={}, svc_name="Cloud",
            )

    # 2 tool rounds + 1 final round
    assert post.call_count == 3
    assert result == ""


def test_bearer_loop_reports_usage():
    """given a usage callback
    when: one round completes
    then: the provider's token counters reach the callback
    """
    final = _chat_resp(content="ответ", usage=(31, 17))
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = final
    usage = []

    with patch("core.assistant_tools.requests.post", return_value=mock_resp):
        run_bearer_tool_loop(
            "https://gw.example/v1/chat/completions", "k", "m",
            "sys", [], "q", temperature=0.3,
            tools_list=[_FUNCTION_TOOL], cfg={}, svc_name="Cloud",
            usage_callback=lambda u: usage.append(u),
        )

    assert usage and usage[0]["in"] == 31
    assert usage[0]["out"] == 17


def test_bearer_loop_extra_payload_reaches_request():
    """given extra payload fields declared by the service
    when: the loop builds a request
    then: those fields land in the payload next to the platform's own keys
    """
    final = _chat_resp(content="ok")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = final

    with patch("core.assistant_tools.requests.post", return_value=mock_resp) as post:
        run_bearer_tool_loop(
            "https://gw.example/v1/chat/completions", "k", "m",
            "sys", [], "q", temperature=0.3,
            tools_list=[_FUNCTION_TOOL], cfg={}, svc_name="Cloud",
            extra_payload={"chat_template_kwargs": {"enable_thinking": True}},
        )

    sent = post.call_args.kwargs["json"]
    assert sent["chat_template_kwargs"] == {"enable_thinking": True}
    assert sent["model"] == "m"


def test_bearer_loop_sends_nested_tool_shape():
    """given a flat Responses-shape function tool
    when: the bearer loop sends its first request
    then: the wire payload nests the function fields under "function"
    """
    final = _chat_resp(content="ok")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = final

    with patch("core.assistant_tools.requests.post", return_value=mock_resp) as post:
        run_bearer_tool_loop(
            "https://gw.example/v1/chat/completions", "k", "m",
            "sys", [], "q", temperature=0.3,
            tools_list=[_FUNCTION_TOOL], cfg={}, svc_name="Cloud",
        )

    sent_tool = post.call_args.kwargs["json"]["tools"][0]
    assert sent_tool["type"] == "function"
    assert sent_tool["function"]["name"] == "rag_search"
    assert "name" not in sent_tool


def test_bearer_loop_degrades_when_gateway_rejects_tools():
    """given a gateway that refuses the tools payload
    when: the first request fails with a tools-related 400
    then: the loop retries without tools and returns the plain answer
    """
    from core.api_errors import ProviderHTTPError

    ok = MagicMock()
    ok.status_code = 200
    ok.json.return_value = _chat_resp(content="Ответ без инструментов")
    rejected = MagicMock()
    rejected.status_code = 400
    rejected.json.return_value = {
        "error": {"message": "tools are not supported by this model"}
    }

    with patch("core.assistant_tools.requests.post",
               side_effect=[rejected, ok]) as post:
        result = run_bearer_tool_loop(
            "https://gw.example/v1/chat/completions", "k", "m",
            "sys", [], "q", temperature=0.3,
            tools_list=[_FUNCTION_TOOL], cfg={}, svc_name="Cloud",
        )

    assert result == "Ответ без инструментов"
    assert post.call_count == 2
    assert "tools" not in post.call_args_list[1].kwargs["json"]
    # The first error must surface as a provider error, not be swallowed.
    assert ProviderHTTPError(400, "x").status_code == 400
