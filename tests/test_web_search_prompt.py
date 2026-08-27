# -*- coding: utf-8 -*-
"""Tests for per-orchestrator web-search config and the ``instructions``
parameter of the ``web_search`` tool.

Covers:
  - ``DEFAULT_WEB_SEARCH_PROMPT`` and the config fallback chain in
    ``core.orchestrators.get_web_search_prompt``.
  - ``get_web_search_config`` returning the prompt together with
    service/model/temperature/max_tool_calls.
  - ``ToolExecutor.web_search``: orchestrator config takes precedence over
    the global DevAgent config, and task-specific ``instructions`` are
    appended to the base prompt.
  - Provider-specific behavior: Yandex forces ``tool_choice``, DeepSeek
    does not (plus strict single-search rule), and empty provider responses
    trigger one retry followed by an explicit error when still empty.

All external HTTP / provider logic is mocked; no network access is needed.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from core.orchestrators import (
    DEFAULT_WEB_SEARCH_PROMPT,
    get_web_search_prompt,
    get_web_search_config,
    DEVAGENT_SLUG,
)


BASE_PROMPT = DEFAULT_WEB_SEARCH_PROMPT


# ── get_web_search_prompt fallback chain ───────────────────────────────────────

def test_default_prompt_is_stable():
    assert isinstance(BASE_PROMPT, str) and len(BASE_PROMPT) > 20
    assert "web search" in BASE_PROMPT.lower()


def test_get_web_search_prompt_returns_default_when_orchestrator_missing():
    with patch("core.orchestrators.get_orchestrator", return_value=None):
        assert get_web_search_prompt("no_such_slug") == BASE_PROMPT


def test_get_web_search_prompt_returns_default_when_key_missing():
    orch = {"config": {"search_service": "Svc"}}
    with patch("core.orchestrators.get_orchestrator", return_value=orch):
        assert get_web_search_prompt("slug") == BASE_PROMPT


def test_get_web_search_prompt_returns_default_when_key_empty():
    orch = {"config": {"web_search_prompt": "   "}}
    with patch("core.orchestrators.get_orchestrator", return_value=orch):
        assert get_web_search_prompt("slug") == BASE_PROMPT


def test_get_web_search_prompt_uses_custom_value():
    orch = {"config": {"web_search_prompt": "Custom prompt."}}
    with patch("core.orchestrators.get_orchestrator", return_value=orch):
        assert get_web_search_prompt("slug") == "Custom prompt."


# ── get_web_search_config shape ───────────────────────────────────────────────

def test_get_web_search_config_includes_prompt():
    orch = {
        "config": {
            "search_service": "Svc",
            "search_model": "m1",
            "search_temperature": 0.1,
            "search_max_tool_calls": 2,
            "web_search_prompt": "Custom prompt.",
        }
    }
    with patch("core.orchestrators.get_orchestrator", return_value=orch):
        cfg = get_web_search_config("slug")
    assert cfg["service"] == "Svc"
    assert cfg["model"] == "m1"
    assert cfg["temperature"] == 0.1
    assert cfg["max_tool_calls"] == 2
    assert cfg["prompt"] == "Custom prompt."


def test_get_web_search_config_uses_default_prompt_when_missing():
    orch = {"config": {"search_service": "Svc", "search_model": "m1"}}
    with patch("core.orchestrators.get_orchestrator", return_value=orch):
        cfg = get_web_search_config("slug")
    assert cfg["prompt"] == BASE_PROMPT


# ── ToolExecutor.web_search ───────────────────────────────────────────────────


def _make_executor():
    """Return a ToolExecutor whose imports are fully mocked (no side effects)."""
    with patch("dev_agent.tool_executor.config", MagicMock()) as mock_conf:
        mock_conf.ensure_runtime_dirs.return_value = None
        mock_conf.PROJECT_ROOT = MagicMock()
        from dev_agent.tool_executor import ToolExecutor
        return ToolExecutor()


def _patch_services(auth_type: str = "", svc_name: str = "OrchSvc"):
    """Patch get_services so web_search sees the requested provider auth_type."""
    return patch(
        "dev_agent.tool_executor.get_services",
        return_value={svc_name: {"auth_type": auth_type}},
    )


def test_web_search_sends_orchestrator_prompt_with_instructions():
    executor = _make_executor()
    executor._web_search_enabled = True
    executor._web_search_config = {
        "service": "OrchSvc",
        "model": "orch_model",
        "temperature": 0.2,
        "max_tool_calls": 1,
        "prompt": "Orchestrator base prompt.",
    }

    calls = {}
    with patch("dev_agent.tool_executor.load_devagent_config", return_value={
        "search_service": "GlobalSvc",
        "search_model": "global_model",
        "search_temperature": "0.7",
    }), _patch_services("", "OrchSvc"), \
         patch("dev_agent.tool_executor.send_request") as mock_send:
        mock_send.return_value = "answer"
        result = executor.web_search(
            query="q",
            instructions="Answer in French.",
            allowed_domains=["example.com"],
            search_context_size="high",
        )
        calls["kwargs"] = mock_send.call_args.kwargs

    assert result["ok"] is True
    assistant = calls["kwargs"]["assistant"]
    # Orchestrator config wins over the global DevAgent config.
    assert assistant["service"] == "OrchSvc"
    assert assistant["model"] == "orch_model"
    assert assistant["temperature"] == 0.2
    # Base prompt + instructions concatenated.
    assert assistant["text"] == "Orchestrator base prompt.\n\nAnswer in French."
    # Tool object carries filters and context size.
    tool_obj = assistant["tools"][0]
    assert tool_obj["type"] == "web_search"
    assert tool_obj["filters"] == {"allowed_domains": ["example.com"]}
    assert tool_obj["search_context_size"] == "high"
    # Unknown auth type: no forced tool_choice.
    assert "tool_choice" not in assistant


def test_web_search_falls_back_to_global_config():
    executor = _make_executor()
    executor._web_search_enabled = True
    executor._web_search_config = None

    calls = {}
    with patch("dev_agent.tool_executor.load_devagent_config", return_value={
        "search_service": "GlobalSvc",
        "search_model": "global_model",
        "search_temperature": "0.7",
    }), patch("dev_agent.tool_executor.send_request") as mock_send, \
         _patch_services("", "GlobalSvc"), \
         patch("core.orchestrators.get_web_search_prompt", return_value="Fallback base prompt."):
        mock_send.return_value = "answer"
        result = executor.web_search(query="q")
        calls["kwargs"] = mock_send.call_args.kwargs

    assert result["ok"] is True
    assistant = calls["kwargs"]["assistant"]
    assert assistant["service"] == "GlobalSvc"
    assert assistant["model"] == "global_model"
    assert assistant["temperature"] == 0.7
    assert assistant["text"] == "Fallback base prompt."


def test_web_search_without_instructions_uses_base_prompt_only():
    executor = _make_executor()
    executor._web_search_enabled = True
    executor._web_search_config = {
        "service": "Svc",
        "model": "m",
        "temperature": 0.3,
        "prompt": "Base prompt.",
    }

    with patch("dev_agent.tool_executor.load_devagent_config", return_value={}), \
         _patch_services("", "Svc"), \
         patch("dev_agent.tool_executor.send_request") as mock_send:
        mock_send.return_value = "answer"
        executor.web_search(query="q")

    assistant = mock_send.call_args.kwargs["assistant"]
    assert assistant["text"] == "Base prompt."


def test_web_search_blocked_when_disabled():
    executor = _make_executor()
    executor._web_search_enabled = False
    result = executor.web_search(query="q")
    assert result["ok"] is False
    assert "disabled" in result["error"].lower()


def test_web_search_returns_error_when_not_configured():
    executor = _make_executor()
    executor._web_search_enabled = True
    executor._web_search_config = None

    with patch("dev_agent.tool_executor.load_devagent_config", return_value={
        "search_service": "",
        "search_model": "",
    }):
        result = executor.web_search(query="q")

    assert result["ok"] is False
    assert "not configured" in result["error"].lower()


# ── Provider-specific behavior ────────────────────────────────────────────────

def test_web_search_yandex_forces_tool_choice():
    executor = _make_executor()
    executor._web_search_enabled = True
    executor._web_search_config = {
        "service": "YandexSvc",
        "model": "m",
        "temperature": 0.3,
        "prompt": "Base prompt.",
    }

    with patch("dev_agent.tool_executor.load_devagent_config", return_value={}), \
         _patch_services("yandex_iam", "YandexSvc"), \
         patch("dev_agent.tool_executor.send_request") as mock_send:
        mock_send.return_value = "answer"
        executor.web_search(query="q")

    assistant = mock_send.call_args.kwargs["assistant"]
    assert assistant["tool_choice"] == {"type": "web_search"}
    assert "exactly ONE web search" not in assistant["text"]


def test_web_search_deepseek_not_forced_and_gets_one_search_rule():
    executor = _make_executor()
    executor._web_search_enabled = True
    executor._web_search_config = {
        "service": "DeepSeek",
        "model": "m",
        "temperature": 0.3,
        "prompt": "Base prompt.",
    }

    with patch("dev_agent.tool_executor.load_devagent_config", return_value={}), \
         _patch_services("deepseek_responses", "DeepSeek"), \
         patch("dev_agent.tool_executor.send_request") as mock_send:
        mock_send.return_value = "answer"
        executor.web_search(query="q")

    assistant = mock_send.call_args.kwargs["assistant"]
    assert "tool_choice" not in assistant
    assert "perform exactly ONE web search" in assistant["text"]
    assert "Never perform additional searches." in assistant["text"]


def test_web_search_yandex_retries_once_without_tool_choice_on_empty():
    executor = _make_executor()
    executor._web_search_enabled = True
    executor._web_search_config = {
        "service": "YandexSvc",
        "model": "m",
        "temperature": 0.3,
        "prompt": "Base prompt.",
    }

    with patch("dev_agent.tool_executor.load_devagent_config", return_value={}), \
         _patch_services("yandex_iam", "YandexSvc"), \
         patch("dev_agent.tool_executor.send_request") as mock_send:
        mock_send.side_effect = ["", "recovered answer"]
        result = executor.web_search(query="q")

    assert result["ok"] is True
    assert mock_send.call_count == 2
    first = mock_send.call_args_list[0].kwargs["assistant"]
    second = mock_send.call_args_list[1].kwargs["assistant"]
    assert first.get("tool_choice") == {"type": "web_search"}
    assert "tool_choice" not in second


def test_web_search_both_empty_returns_explicit_error():
    executor = _make_executor()
    executor._web_search_enabled = True
    executor._web_search_config = {
        "service": "YandexSvc",
        "model": "m",
        "temperature": 0.3,
        "prompt": "Base prompt.",
    }

    with patch("dev_agent.tool_executor.load_devagent_config", return_value={}), \
         _patch_services("yandex_iam", "YandexSvc"), \
         patch("dev_agent.tool_executor.send_request") as mock_send:
        mock_send.side_effect = ["", ""]
        result = executor.web_search(query="q")

    assert result["ok"] is False
    assert "empty response" in result["error"].lower()
    assert mock_send.call_count == 2


# ── Catalog entry ─────────────────────────────────────────────────────────────

def test_catalog_web_search_mentions_instructions():
    from dev_agent.tool_executor import TOOL_CATALOG
    entry = next(t for t in TOOL_CATALOG if t["name"] == "web_search")
    assert "instructions" in entry["desc"]
    assert "base system prompt" in entry["desc"]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
