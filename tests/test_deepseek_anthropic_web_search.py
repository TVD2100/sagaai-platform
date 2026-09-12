# -*- coding: utf-8 -*-
"""Tests for server-side DeepSeek web search via the Anthropic-compatible API.

DeepSeek ignores the built-in web_search tool on the OpenAI-compatible
/responses route (HTTP 200, no search executed), so web_search for DeepSeek
routes through https://api.deepseek.com/anthropic/v1/messages with the
``web_search_20250305`` server tool.

Covers:
  - _deepseek_anthropic_web_search payload shape (model, max_tokens,
    system prompt with allowed domains, server tool, user messages).
  - Headers (x-api-key + anthropic-version) and endpoint.
  - Detection: a response with server_tool_use / web_search_tool_result
    blocks counts as a real search (fixture mirrors a real DeepSeek response).
  - Text extraction: only the LAST text block is returned; ``thinking`` and
    tool blocks are never leaked.
  - Retry: when the first response contains no search blocks, the request is
    re-sent once with a reinforced user message; a second still-searching
    (or searchless) response yields an explicit error, never a fake success.
  - max_uses mapping: search_context_size low/medium/high -> 1/2/3.

All HTTP is mocked; no external network access.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.api_layer import (
    _deepseek_anthropic_web_search,
    _anthropic_web_search_used,
    _extract_anthropic_text,
)


WS_URL = "https://api.deepseek.com/anthropic/v1/messages"


# ── Fixture helpers ──────────────────────────────────────────────────────────


def _search_response(final_text: str = "Final answer.") -> dict:
    """Return an Anthropic Messages response with real search activity.

    The shape mirrors the live DeepSeek response captured during diagnosis:
    thinking / announcements / server_tool_use / web_search_tool_result
    blocks, ending with the final text block.
    """
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [
            {"type": "thinking", "thinking": "I must search the web."},
            {"type": "text", "text": "I'll search the web now."},
            {
                "type": "server_tool_use",
                "id": "call_00",
                "name": "web_search",
                "input": {"query": "top news today"},
                "caller": {"type": "direct"},
            },
            {
                "type": "web_search_tool_result",
                "tool_use_id": "call_00",
                "content": [
                    {
                        "type": "web_search_result",
                        "title": "World | The Daily Star",
                        "url": "https://images.thedailystar.net/news/world",
                    },
                ],
            },
            {"type": "thinking", "thinking": "Summarize the results."},
            {"type": "text", "text": final_text},
        ],
    }


def _no_search_response() -> dict:
    """Return a response with NO search activity (model answered from memory)."""
    return {
        "id": "msg_02",
        "type": "message",
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [
            {"type": "thinking", "thinking": "I can answer without search."},
            {"type": "text", "text": "Answer from memory."},
        ],
    }


def _ok_resp(body: dict):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = body
    return resp


# ── Block-level helpers ──────────────────────────────────────────────────────


def test_anthropic_web_search_used_detects_search_blocks():
    assert _anthropic_web_search_used(_search_response()) is True
    assert _anthropic_web_search_used(_no_search_response()) is False
    assert _anthropic_web_search_used({}) is False
    assert _anthropic_web_search_used({"content": []}) is False


def test_extract_anthropic_text_returns_last_text_block_only():
    text = _extract_anthropic_text(_search_response("Итоговый ответ"))
    assert text == "Итоговый ответ"
    assert "I'll search" not in text
    assert "internal" not in text


def test_extract_anthropic_text_empty():
    assert _extract_anthropic_text({}) == ""
    assert _extract_anthropic_text({"content": []}) == ""
    assert _extract_anthropic_text({
        "content": [{"type": "thinking", "thinking": "x"}]
    }) == ""


# ── _deepseek_anthropic_web_search (mocked HTTP) ─────────────────────────────


def test_payload_shape_and_headers():
    with patch("core.api_layer.requests.post") as post:
        post.return_value = _ok_resp(_search_response("Done."))
        result = _deepseek_anthropic_web_search(
            base_url=WS_URL,
            api_key="sk-test",
            model="deepseek-flash",
            system_prompt="You are a search agent.",
            query="What is the news today?",
            allowed_domains=["example.com", "example.org"],
            search_context_size="high",
        )

    assert result["ok"] is True
    assert result["text"] == "Done."

    args, kwargs = post.call_args
    assert args[0] == WS_URL
    headers = kwargs["headers"]
    assert headers["x-api-key"] == "sk-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["content-type"] == "application/json"

    payload = kwargs["json"]
    assert payload["model"] == "deepseek-flash"
    assert payload["max_tokens"] == 4096
    # allowed_domains are inlined into the system prompt (no native filter).
    assert "example.com" in payload["system"]
    assert "example.org" in payload["system"]
    # Server tool with max_uses=3 for high search depth.
    assert payload["tools"] == [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
    ]
    assert "tool_choice" not in payload
    assert payload["messages"] == [{"role": "user", "content": "What is the news today?"}]


def test_no_tool_choice_in_payload():
    """Forcing tool selection fails on DeepSeek (HTTP 400), so it is never sent."""
    with patch("core.api_layer.requests.post") as post:
        post.return_value = _ok_resp(_search_response("Done."))
        _deepseek_anthropic_web_search(
            base_url=WS_URL, api_key="k", model="m",
            system_prompt="", query="q",
        )
    assert "tool_choice" not in post.call_args.kwargs["json"]


def test_max_uses_mapping_by_search_context_size():
    with patch("core.api_layer.requests.post") as post:
        post.return_value = _ok_resp(_search_response("Done."))
        seen = {}
        for ctx in ("low", "medium", "high", "bogus", None):
            _deepseek_anthropic_web_search(
                base_url=WS_URL, api_key="k", model="m",
                system_prompt="", query="q", search_context_size=ctx,
            )
            seen[ctx] = post.call_args.kwargs["json"]["tools"][0]["max_uses"]
    assert seen == {"low": 1, "medium": 2, "high": 3, "bogus": 2, None: 2}


def test_retry_once_when_first_response_has_no_search():
    responses = [_ok_resp(_no_search_response()), _ok_resp(_search_response("Recovered."))]
    with patch("core.api_layer.requests.post", side_effect=responses) as post:
        result = _deepseek_anthropic_web_search(
            base_url=WS_URL, api_key="k", model="m",
            system_prompt="", query="q",
        )

    assert result["ok"] is True
    assert result["text"] == "Recovered."
    assert post.call_count == 2
    second_user = post.call_args_list[1].kwargs["json"]["messages"][0]["content"]
    assert "MUST use the web_search tool" in second_user
    assert second_user.startswith("q\n")


def test_explicit_error_when_both_attempts_have_no_search():
    responses = [_ok_resp(_no_search_response()), _ok_resp(_no_search_response())]
    with patch("core.api_layer.requests.post", side_effect=responses) as post:
        result = _deepseek_anthropic_web_search(
            base_url=WS_URL, api_key="k", model="m",
            system_prompt="", query="q",
        )

    assert result["ok"] is False
    assert "did not execute the web search" in result["error"]
    assert post.call_count == 2


def test_second_attempt_searched_but_empty_answer():
    """Search blocks present but empty final text: same signature as a
    plain successful response; the emptiness check lives in the caller."""
    empty_final = _search_response(final_text="")
    responses = [_ok_resp(_no_search_response()), _ok_resp(empty_final)]
    with patch("core.api_layer.requests.post", side_effect=responses):
        result = _deepseek_anthropic_web_search(
            base_url=WS_URL, api_key="k", model="m",
            system_prompt="", query="q",
        )
    assert result["ok"] is True
    assert result["text"] == ""


def test_provider_http_error_is_raised():
    from core.api_errors import ProviderHTTPError

    resp = MagicMock()
    resp.status_code = 401
    with patch("core.api_layer.requests.post", return_value=resp):
        try:
            _deepseek_anthropic_web_search(
                base_url=WS_URL, api_key="bad", model="m",
                system_prompt="", query="q",
            )
            raised = False
        except ProviderHTTPError as e:
            raised = True
            assert e.status_code == 401
    assert raised


def test_network_error_is_raised():
    import requests as _requests
    from core.api_errors import NetworkError

    with patch("core.api_layer.requests.post",
               side_effect=_requests.exceptions.ConnectionError("down")):
        try:
            _deepseek_anthropic_web_search(
                base_url=WS_URL, api_key="k", model="m",
                system_prompt="", query="q",
            )
            raised = False
        except NetworkError:
            raised = True
    assert raised


# ── Integration with ToolExecutor (deepseek_responses branch) ────────────────


def test_web_search_deepseek_routes_to_anthropic_endpoint():
    """ToolExecutor.web_search must NOT call send_request for DeepSeek:
    it routes to the Anthropic-compatible endpoint instead."""
    with patch("dev_agent.tool_executor.config", MagicMock()) as mock_conf:
        mock_conf.ensure_runtime_dirs.return_value = None
        mock_conf.PROJECT_ROOT = MagicMock()
        from dev_agent.tool_executor import ToolExecutor
        executor = ToolExecutor()
    executor._web_search_enabled = True
    executor._web_search_config = {
        "service": "DeepSeek",
        "model": "m",
        "temperature": 0.3,
        "prompt": "Base prompt.",
    }

    with patch("dev_agent.tool_executor.load_devagent_config", return_value={}), \
         patch("dev_agent.tool_executor.get_services", return_value={"DeepSeek": {
             "auth_type": "deepseek_responses",
             "config_key": "DEEPSEEK_API_KEY",
             "base_url": "https://api.deepseek.com/responses",
         }}), \
         patch("dev_agent.tool_executor.send_request") as mock_send, \
         patch("dev_agent.tool_executor.load_config", return_value={"DEEPSEEK_API_KEY": "k"}), \
         patch("core.api_layer._deepseek_anthropic_web_search") as mock_ws:
        mock_ws.return_value = {"ok": True, "text": "From search."}
        result = executor.web_search(query="q", allowed_domains=["example.com"])

    assert result["ok"] is True
    assert result["text"].startswith("[DATA_FROM_WEB_SEARCH]")
    assert "From search." in result["text"]
    mock_send.assert_not_called()
    kwargs = mock_ws.call_args.kwargs
    assert kwargs["api_key"] == "k"
    assert kwargs["model"] == "m"
    assert kwargs["allowed_domains"] == ["example.com"]
    assert kwargs["base_url"] == "https://api.deepseek.com/anthropic/v1/messages"
    assert "exactly ONE web search" in kwargs["system_prompt"]


def test_web_search_deepseek_missing_key_returns_error():
    with patch("dev_agent.tool_executor.config", MagicMock()) as mock_conf:
        mock_conf.ensure_runtime_dirs.return_value = None
        mock_conf.PROJECT_ROOT = MagicMock()
        from dev_agent.tool_executor import ToolExecutor
        executor = ToolExecutor()
    executor._web_search_enabled = True
    executor._web_search_config = {
        "service": "DeepSeek",
        "model": "m",
        "temperature": 0.3,
        "prompt": "Base prompt.",
    }

    with patch("dev_agent.tool_executor.load_devagent_config", return_value={}), \
         patch("dev_agent.tool_executor.get_services", return_value={"DeepSeek": {
             "auth_type": "deepseek_responses",
             "config_key": "DEEPSEEK_API_KEY",
             "base_url": "https://api.deepseek.com/responses",
         }}), \
         patch("dev_agent.tool_executor.load_config", return_value={}):
        result = executor.web_search(query="q")

    assert result["ok"] is False
    assert "not configured" in result["error"].lower()


def test_web_search_deepseek_uses_web_search_base_url_field():
    with patch("dev_agent.tool_executor.config", MagicMock()) as mock_conf:
        mock_conf.ensure_runtime_dirs.return_value = None
        mock_conf.PROJECT_ROOT = MagicMock()
        from dev_agent.tool_executor import ToolExecutor
        executor = ToolExecutor()
    executor._web_search_enabled = True
    executor._web_search_config = {
        "service": "DeepSeek",
        "model": "m",
        "temperature": 0.3,
        "prompt": "Base prompt.",
    }

    with patch("dev_agent.tool_executor.load_devagent_config", return_value={}), \
         patch("dev_agent.tool_executor.get_services", return_value={"DeepSeek": {
             "auth_type": "deepseek_responses",
             "config_key": "DEEPSEEK_API_KEY",
             "base_url": "https://api.deepseek.com/responses",
             "web_search_base_url": "https://api.deepseek.com/anthropic/v1/messages",
         }}), \
         patch("dev_agent.tool_executor.load_config", return_value={"DEEPSEEK_API_KEY": "k"}), \
         patch("core.api_layer._deepseek_anthropic_web_search") as mock_ws:
        mock_ws.return_value = {"ok": True, "text": "ok"}
        executor.web_search(query="q")

    assert mock_ws.call_args.kwargs["base_url"] == "https://api.deepseek.com/anthropic/v1/messages"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-q"])
