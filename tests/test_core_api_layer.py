"""
tests.test_core_api_layer - unit tests for core.api_layer.

Tests for pure functions that do NOT require network mocking:
_normalise_tools, _extract_responses_text, _prepare_response_content,
_gigachat_verify.

Tests requiring network mocks (send_request, _bearer_request, etc.)
are in test_core_api_send.py.
"""

import json
import pytest


# ─── _normalise_tools ──────────────────────────────────────────────────────

def test_normalise_tools_strings():
    from core.api_layer import _normalise_tools
    tools = ["web_search", "code_interpreter"]
    expected = [
        {"type": "web_search"},
        {"type": "code_interpreter"},
    ]
    assert _normalise_tools(tools) == expected


def test_normalise_tools_dicts():
    from core.api_layer import _normalise_tools
    tools = [
        {"type": "web_search", "filters": {"allowed_domains": ["python.org"]}},
        {"type": "code_interpreter"},
    ]
    assert _normalise_tools(tools) == tools


def test_normalise_tools_mixed():
    from core.api_layer import _normalise_tools
    tools = [
        "web_search",
        {"type": "code_interpreter", "max_tool_calls": 5},
        "python",
    ]
    expected = [
        {"type": "web_search"},
        {"type": "code_interpreter", "max_tool_calls": 5},
        {"type": "python"},
    ]
    assert _normalise_tools(tools) == expected


def test_normalise_tools_empty():
    from core.api_layer import _normalise_tools
    assert _normalise_tools([]) == []
    assert _normalise_tools(None) == []


# ─── _extract_responses_text ───────────────────────────────────────────────

def test_extract_responses_text_from_output_text():
    from core.api_layer import _extract_responses_text
    data = {"output_text": "Привет, мир!"}
    assert _extract_responses_text(data) == "Привет, мир!"


def test_extract_responses_text_from_output_blocks():
    from core.api_layer import _extract_responses_text
    data = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"text": "Первый блок."},
                    {"text": "Второй блок."}
                ]
            },
            {"type": "other", "content": [{"text": "игнорируется"}]}
        ]
    }
    assert _extract_responses_text(data) == "Первый блок.\n\nВторой блок."


def test_extract_responses_text_empty():
    from core.api_layer import _extract_responses_text
    assert _extract_responses_text({}) == ""
    assert _extract_responses_text({"output_text": ""}) == ""
    assert _extract_responses_text({"output": []}) == ""
    assert _extract_responses_text({"output": [{"type": "other", "content": []}]}) == ""


# ─── _prepare_response_content ─────────────────────────────────────────────

def test_prepare_response_content_text_only():
    from core.api_layer import _prepare_response_content
    msg = {"content": "Ответ модели"}
    assert _prepare_response_content(msg) == "Ответ модели"


def test_prepare_response_content_ignores_reasoning_when_content_empty():
    """DeepSeek thinking mode: reasoning_content must never leak.

    Regression: _prepare_response_content used to fall back to
    'reasoning_content' when 'content' was empty, which surfaced the
    model's chain-of-thought in the UI and in parse_tool_calls.
    """
    from core.api_layer import _prepare_response_content
    msg = {"content": "", "reasoning_content": "internal thought"}
    assert _prepare_response_content(msg) == ""


def test_prepare_response_content_ignores_reasoning_when_content_present():
    """reasoning_content is dropped even when content exists."""
    from core.api_layer import _prepare_response_content
    msg = {"content": "Ответ модели", "reasoning_content": "internal thought"}
    result = _prepare_response_content(msg)
    assert result == "Ответ модели"
    assert "internal thought" not in result


def test_prepare_response_content_tool_calls_only():
    from core.api_layer import _prepare_response_content
    msg = {
        "tool_calls": [
            {
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "README.md"}'
                }
            }
        ]
    }
    result = _prepare_response_content(msg)
    assert "read_file" in result
    assert "README.md" in result
    assert result.startswith("```json")


def test_prepare_response_content_both():
    from core.api_layer import _prepare_response_content
    msg = {
        "content": "Проверяю файл.",
        "tool_calls": [
            {
                "function": {
                    "name": "list_files",
                    "arguments": '{}'
                }
            }
        ]
    }
    result = _prepare_response_content(msg)
    assert "Проверяю файл." in result
    assert "list_files" in result


def test_prepare_response_content_empty():
    from core.api_layer import _prepare_response_content
    assert _prepare_response_content({}) == ""
    assert _prepare_response_content({"content": ""}) == ""
    assert _prepare_response_content({"content": "   "}) == ""


def test_prepare_response_content_broken_tool_args():
    from core.api_layer import _prepare_response_content
    msg = {
        "tool_calls": [
            {
                "function": {
                    "name": "bad_tool",
                    "arguments": "not-json"
                }
            }
        ]
    }
    result = _prepare_response_content(msg)
    assert "bad_tool" in result
    assert "args" in result


# ─── _gigachat_verify ──────────────────────────────────────────────────────

def test_gigachat_verify_honours_global_disable(monkeypatch):
    from core import api_layer
    monkeypatch.setattr(api_layer, "_VERIFY_TLS", False)
    assert api_layer._gigachat_verify() is False


def test_gigachat_verify_returns_default_bundle_when_present(monkeypatch, tmp_path):
    from core import api_layer
    bundle = tmp_path / "ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setattr(api_layer, "_VERIFY_TLS", True)
    monkeypatch.setattr(api_layer, "_GIGACHAT_DEFAULT_CA_BUNDLE", str(bundle))
    monkeypatch.delenv("SAGAAI_GIGACHAT_CA_BUNDLE", raising=False)
    assert api_layer._gigachat_verify() == str(bundle)


def test_gigachat_verify_honours_env_bundle(monkeypatch, tmp_path):
    from core import api_layer
    bundle = tmp_path / "env.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setattr(api_layer, "_VERIFY_TLS", True)
    monkeypatch.setenv("SAGAAI_GIGACHAT_CA_BUNDLE", str(bundle))
    assert api_layer._gigachat_verify() == str(bundle)


def test_gigachat_verify_falls_back_when_bundle_missing(monkeypatch, tmp_path):
    from core import api_layer
    missing = str(tmp_path / "missing.pem")
    monkeypatch.setattr(api_layer, "_VERIFY_TLS", True)
    monkeypatch.setattr(api_layer, "_GIGACHAT_DEFAULT_CA_BUNDLE", missing)
    monkeypatch.delenv("SAGAAI_GIGACHAT_CA_BUNDLE", raising=False)
    assert api_layer._gigachat_verify() is True
