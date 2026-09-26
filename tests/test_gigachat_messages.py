# -*- coding: utf-8 -*-
"""tests.test_gigachat_messages - GigaChat request-shape and max_tokens tests.

The GigaChat provider accepts AT MOST ONE system message and it must be the
FIRST element of ``messages`` (HTTP 422 otherwise). The orchestrator loop
injects service system blocks (economy-mode metadata, external task state,
thread context) into the middle of the history, which used to break every
GigaChat-orchestrated dialog. These tests pin the normalisation done by
``_gigachat_messages`` and the per-model max_tokens clamp in ``send_request``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import core.api_layer as api_layer
from core.api_layer import _clamp_max_tokens, _gigachat_messages


GIGACHAT_SVC = {
    "name": "GigaChat",
    "auth_type": "gigachat_oauth",
    "base_url": "https://api.giga.chat/v1/chat/completions",
    "config_key": "GIGACHAT_API_KEY",
    "config_key2": "GIGACHAT_SCOPE",
    "context_window_default": 128000,
    "max_tokens_default": 32768,
    "models": [
        {"id": "GigaChat-3-Pro", "context_window": 128000, "max_tokens": 32768},
        {"id": "GigaChat-2", "context_window": 128000, "max_tokens": 32768},
    ],
}

DEEPSEEK_SVC = {
    "name": "DeepSeek",
    "auth_type": "deepseek_responses",
    "base_url": "https://api.deepseek.example/v1",
    "config_key": "DEEPSEEK_API_KEY",
    "context_window_default": 1048576,
    "max_tokens_default": 384000,
    "models": [
        {"id": "deepseek-v4-pro", "context_window": 1048576, "max_tokens": 384000},
    ],
}


# --- _gigachat_messages -----------------------------------------------------

def test_single_leading_system_message_folds_history_system_blocks():
    """One system message, first; in-history system blocks are folded into it."""
    hist = [
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "ECONOMY MODE: ENABLED"},
        {"role": "assistant", "content": "hello"},
        {"role": "system", "content": "CURRENT TASK STATE"},
    ]
    msgs = _gigachat_messages("SYS PROMPT", hist, "question")

    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].startswith("SYS PROMPT")
    assert "ECONOMY MODE: ENABLED" in msgs[0]["content"]
    assert "CURRENT TASK STATE" in msgs[0]["content"]
    assert sum(1 for m in msgs if m["role"] == "system") == 1
    assert [m["role"] for m in msgs[1:]] == ["user", "assistant", "user"]


def test_consecutive_same_roles_are_merged():
    """GigaChat rejects consecutive same-role messages: they are joined."""
    hist = [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": "c"},
        {"role": "assistant", "content": "d"},
        {"role": "user", "content": "e"},
    ]
    msgs = _gigachat_messages("", hist, "f")

    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == "a\n\nb"
    assert msgs[1]["content"] == "c\n\nd"
    assert msgs[2]["content"] == "e\n\nf"


def test_no_system_message_when_prompt_empty():
    msgs = _gigachat_messages("", [], "only user")
    assert [m["role"] for m in msgs] == ["user"]
    assert msgs[0]["content"] == "only user"


def test_ui_only_keys_are_stripped():
    """Only role/content reach the wire: hidden/_index/_events are dropped."""
    hist = [
        {"role": "system", "content": "ctx", "hidden": True, "_index": -2},
        {"role": "user", "content": "x", "_events": [{"type": "error"}]},
    ]
    msgs = _gigachat_messages("S", hist, "y")
    assert all(set(m.keys()) == {"role", "content"} for m in msgs)


# --- _clamp_max_tokens ------------------------------------------------------

def test_clamp_caps_gigachat_model_limit():
    assert _clamp_max_tokens(384000, GIGACHAT_SVC, "GigaChat-3-Pro") == 32768


def test_clamp_keeps_value_below_limit():
    assert _clamp_max_tokens(8192, GIGACHAT_SVC, "GigaChat-3-Pro") == 8192


def test_clamp_uses_service_default_for_unknown_model():
    svc = {"models": [], "max_tokens_default": 32768}
    assert _clamp_max_tokens(100000, svc, "unknown-model") == 32768


def test_clamp_passthrough_without_explicit_limit():
    svc = {"models": [{"id": "m"}]}
    assert _clamp_max_tokens(999999, svc, "m") == 999999


def test_clamp_deepseek_limit_unchanged():
    assert _clamp_max_tokens(384000, DEEPSEEK_SVC, "deepseek-v4-pro") == 384000


# --- send_request integration -----------------------------------------------

def _run_send(monkeypatch, svc_name, svc, assistant, history=None):
    """Run send_request with a stubbed transport; return captured kwargs."""
    called = {}

    def _fake_do_request(**kwargs):
        called["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(api_layer, "_do_request", _fake_do_request)
    monkeypatch.setattr(api_layer, "get_services", lambda: {svc_name: svc})
    monkeypatch.setattr(api_layer, "load_config", lambda: {})
    monkeypatch.setattr(api_layer, "load_assistant_files_context", lambda *a, **k: "")

    out = api_layer.send_request("hi", assistant, history=history or [])
    assert out == "ok"
    return called["kwargs"]


def test_send_request_clamps_gigachat_max_tokens(monkeypatch):
    """A 384000 value saved for DeepSeek must be clamped for GigaChat."""
    kwargs = _run_send(monkeypatch, "GigaChat", GIGACHAT_SVC, {
        "service": "GigaChat", "model": "GigaChat-3-Pro",
        "temperature": 0.3, "text": "Sys", "max_tokens": 384000,
    })
    assert kwargs["max_tokens"] == 32768


def test_send_request_keeps_deepseek_max_tokens(monkeypatch):
    kwargs = _run_send(monkeypatch, "DeepSeek", DEEPSEEK_SVC, {
        "service": "DeepSeek", "model": "deepseek-v4-pro",
        "temperature": 0.3, "text": "Sys", "max_tokens": 384000,
    })
    assert kwargs["max_tokens"] == 384000


def test_gigachat_wire_payload_has_single_leading_system_and_clamped_tokens():
    """End-to-end through the real GigaChat branch with a mocked HTTP layer."""
    with patch("core.api_layer.get_services", return_value={"GigaChat": GIGACHAT_SVC}), \
         patch("core.api_layer.load_config",
               return_value={"GIGACHAT_API_KEY": "creds", "GIGACHAT_SCOPE": "s"}), \
         patch("core.api_layer.load_assistant_files_context", return_value=""), \
         patch("core.api_layer._gigachat_token", return_value="tok"), \
         patch("core.api_layer.requests.Session") as session_cls:
        session = MagicMock()
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        session.post.return_value = resp
        session_cls.return_value = session

        out = api_layer.send_request(
            "new question",
            {"service": "GigaChat", "model": "GigaChat-3-Pro", "temperature": 0.3,
             "text": "SYS PROMPT", "max_tokens": 384000},
            history=[
                {"role": "user", "content": "old"},
                {"role": "system", "content": "ECONOMY MODE: ENABLED"},
                {"role": "assistant", "content": "old answer"},
                {"role": "system", "content": "CURRENT TASK STATE"},
            ],
        )

    assert out == "ok"
    payload = session.post.call_args[1]["json"]
    msgs = payload["messages"]
    assert msgs[0]["role"] == "system"
    assert "SYS PROMPT" in msgs[0]["content"]
    assert "ECONOMY MODE: ENABLED" in msgs[0]["content"]
    assert "CURRENT TASK STATE" in msgs[0]["content"]
    assert not any(m["role"] == "system" for m in msgs[1:])
    assert payload["max_tokens"] == 32768
