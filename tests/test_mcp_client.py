# -*- coding: utf-8 -*-
"""tests.test_mcp_client - unit tests for core.mcp_client (Л-4.3).

Every scenario runs against the stdlib-only demo MCP server from
``tests/fixtures/mcp_demo_server.py`` started as a real subprocess:
start/handshake (plus protocol-version mismatch), tools/list with the
per-process cache, successful tools/call, tool-level errors (isError),
JSON-RPC error objects, rejection of non-text results, the hard timeout
with process termination and connection lockout, broken commands, stderr
forwarding, and the process registry (one live process per connection,
replacement kills the old one, no leaks after close).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from core import mcp_client
from core.mcp_client import (
    McpClient,
    McpClientError,
    McpHttpClient,
    McpServerStartError,
    McpTimeoutError,
    _PROCESS_REGISTRY,
    _http_error_hint,
    _resolve_http_auth_header,
    get_client,
    translate_schema_parameters,
)


FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mcp_demo_server.py"


def _start(*extra_args: str, key: str = "test-key",
           stderr_handler=None) -> McpClient:
    """Start and handshake a client against the demo server.

    Positional arguments become the server's argv (state file, protocol
    version override); ``key`` is the registry entry of the client."""
    client = McpClient(sys.executable, [str(FIXTURE_SERVER)] +
                       list(extra_args), server_key=key,
                       stderr_handler=stderr_handler)
    client.start()
    return client


@pytest.fixture(autouse=True)
def _cleanup_process_registry():
    """Drop every live MCP client after each test - no cross-test leaks."""
    yield
    for key in list(_PROCESS_REGISTRY):
        mcp_client.drop_client(key)


# ─── start / handshake ─────────────────────────────────────────────────────


def test_start_handshake_and_list_tools(tmp_path):
    state = tmp_path / "state.json"
    client = _start(str(state))
    try:
        result = client.list_tools()
        assert [t["name"] for t in result["tools"]] == [
            "echo", "fail", "image", "slow"]
        assert client._cached_server_info == {"name": "demo", "version": "1"}
        assert json.loads(state.read_text(encoding="utf-8"))["list"] == 1
    finally:
        client.close()


def test_start_wrong_protocol_version():
    client = McpClient(sys.executable, [str(FIXTURE_SERVER), "",
                                        "2099-12-31"],
                       server_key="bad-version")
    with pytest.raises(McpServerStartError) as exc:
        client.start()
    assert "version" in str(exc.value)
    assert "bad-version" not in _PROCESS_REGISTRY
    assert client.process is None or client.process.poll() is not None


def test_start_broken_command_does_not_crash_platform():
    client = McpClient("definitely-not-a-real-binary-xyz",
                       server_key="broken-command")
    with pytest.raises(McpServerStartError) as exc:
        client.start()
    assert "not found" in str(exc.value)
    assert "broken-command" not in _PROCESS_REGISTRY


# ─── tools/list cache ──────────────────────────────────────────────────────


def test_tools_list_cache_does_not_hit_server_twice(tmp_path):
    state = tmp_path / "state.json"
    client = _start(str(state))
    try:
        first = client.list_tools()
        second = client.list_tools()
        assert first is second  # cached object, process still alive
        assert json.loads(state.read_text(encoding="utf-8"))["list"] == 1
    finally:
        client.close()


# ─── tools/call ────────────────────────────────────────────────────────────


def test_call_tool_success():
    client = _start()
    try:
        assert client.call_tool("echo", {"text": "hi"}) == {
            "content": "echo:hi", "is_error": False}
    finally:
        client.close()


def test_call_tool_is_error_flag():
    client = _start()
    try:
        result = client.call_tool("fail")
        assert result["is_error"] is True
        assert result["content"] == "boom"
    finally:
        client.close()


def test_call_tool_unknown_tool_rpc_error():
    client = _start()
    try:
        with pytest.raises(McpClientError) as exc:
            client.call_tool("nope")
        text = str(exc.value)
        assert "[-32602]" in text
        assert "unknown tool" in text
        assert "nope" in text
    finally:
        client.close()


def test_call_tool_rejects_non_text_result():
    client = _start()
    try:
        with pytest.raises(McpClientError) as exc:
            client.call_tool("image")
        assert "non-text" in str(exc.value)
        assert client.call_tool("echo", {"text": "ok"})["content"] == \
            "echo:ok"
    finally:
        client.close()


def test_call_tool_rejects_non_object_arguments():
    client = _start()
    try:
        with pytest.raises(McpClientError) as exc:
            client.call_tool("echo", [1, 2])
        assert "JSON object" in str(exc.value)
    finally:
        client.close()


def test_call_to_dead_process_fails():
    client = _start()
    client.close()
    with pytest.raises(McpClientError):
        client.call_tool("echo", {"text": "x"})


def test_call_tool_after_server_crash_is_isolated():
    client = _start()
    try:
        pid = client.process.pid
        # Kill the server directly: the platform call must fail cleanly.
        client.process.kill()
        client.process.wait(timeout=5)
        with pytest.raises(McpClientError):
            client.call_tool("echo", {"text": "x"})
        assert pid is not None
    finally:
        client.close()


# ─── timeout ───────────────────────────────────────────────────────────────


def test_timeout_kills_process_and_disables_connection():
    client = _start(key="timeout-key")
    try:
        t0 = time.monotonic()
        with pytest.raises(McpTimeoutError):
            client.call_tool("slow", {}, timeout=2.0)
        assert time.monotonic() - t0 < 15
        assert client.dead is True
        assert client.process is not None
        assert client.process.poll() is not None  # terminated for real
        with pytest.raises(McpClientError) as exc:
            client.call_tool("echo", {"text": "x"})
        assert "recreate" in str(exc.value)
        assert "timeout-key" not in _PROCESS_REGISTRY
    finally:
        client.close()


# ─── process registry / isolation ──────────────────────────────────────────


def test_process_registry_replacement_kills_previous_process():
    first = _start(key="conn-1")
    try:
        second = _start(key="conn-1")
        assert first.process.poll() is not None  # old process was killed
        assert _PROCESS_REGISTRY["conn-1"] is second
        second.close()
        assert "conn-1" not in _PROCESS_REGISTRY
    finally:
        first.close()


def test_no_process_leaks_after_close():
    client = _start(key="leak-check")
    client.close()
    assert _PROCESS_REGISTRY == {}
    assert client.process.poll() is not None


def test_get_client_reuses_live_and_recreates_after_drop():
    c1 = get_client("gc-key", sys.executable, [str(FIXTURE_SERVER)])
    try:
        c2 = get_client("gc-key", sys.executable, [str(FIXTURE_SERVER)])
        assert c1 is c2
    finally:
        mcp_client.drop_client("gc-key")
    c3 = get_client("gc-key", sys.executable, [str(FIXTURE_SERVER)])
    try:
        assert c3 is not c1
        assert c3.started and not c3.dead
    finally:
        mcp_client.drop_client("gc-key")
    assert "gc-key" not in _PROCESS_REGISTRY


# ─── stderr handling ───────────────────────────────────────────────────────


def test_stderr_is_forwarded_to_handler():
    lines: list = []
    client = _start(key="stderr-test", stderr_handler=lines.append)
    try:
        assert client.list_tools()["tools"]
    finally:
        client.close()
    assert any("mcp-demo-server started" in line for line in lines)


# ─── schema translation ────────────────────────────────────────────────────


def test_translate_schema_parameters():
    params = translate_schema_parameters({
        "type": "object",
        "properties": {
            "count": {"type": "integer", "description": "N"},
            "label": {"type": "string"},
            "ok": {"type": "boolean"},
            "items": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["count", "ok"],
    })
    assert params == [
        {"name": "count", "type": "number", "required": True,
         "description": "N"},
        {"name": "label", "type": "string", "required": False,
         "description": ""},
        {"name": "ok", "type": "boolean", "required": True,
         "description": ""},
        {"name": "items", "type": "array", "required": False,
         "description": ""},
    ]


def test_translate_schema_no_properties():
    assert translate_schema_parameters({"type": "object"}) == []
    assert translate_schema_parameters(None) == []
    assert translate_schema_parameters({}) == []


def test_translate_schema_untrusted_shapes_do_not_crash():
    params = translate_schema_parameters({
        "type": "object",
        "properties": {
            "a": "not-a-dict",
            "b": {"type": ["number", "null"]},
            "c": {"type": "weird-type"},
        },
        "required": "not-a-list",
    })
    by_name = {p["name"]: p for p in params}
    assert by_name["a"]["type"] == "string"
    assert by_name["b"]["type"] == "number"
    assert by_name["c"]["type"] == "string"
    assert by_name["c"]["required"] is False


# ─── http auth header resolution ────────────────────────────────────────────


def test_resolve_http_auth_header_empty_placeholders_yield_none():
    assert _resolve_http_auth_header("Bearer {TOKEN}", {}) is None
    assert _resolve_http_auth_header("", {}) is None
    assert _resolve_http_auth_header(
        "Bearer {TOKEN}", {"TOKEN": ""}) is None
    assert _resolve_http_auth_header(
        "Bearer {TOKEN}", {"TOKEN": "  "}) is None
    assert _resolve_http_auth_header(
        "Bearer {TOKEN}", {"OTHER": "x"}) is None


def test_resolve_http_auth_header_filled_placeholders():
    assert _resolve_http_auth_header(
        "Bearer {TOKEN}", {"TOKEN": "abc"}
    ) == "Authorization: Bearer abc"
    assert _resolve_http_auth_header(
        "X-Key: {KEY}", {"KEY": "v"}
    ) == "X-Key: v"
    assert _resolve_http_auth_header(
        "Authorization: Bearer {TOKEN}", {"TOKEN": "t"}
    ) == "Authorization: Bearer t"


# ─── http auth-failure hints (GitHub MCP 401/403) ───────────────────────────

def test_http_error_hint_covers_auth_failures():
    assert "credentials" in _http_error_hint(401)
    hint_403 = _http_error_hint(403)
    assert "fine-grained" in hint_403
    assert "ghp_" in hint_403
    assert "Copilot" in hint_403
    assert "rate-limited" in _http_error_hint(429)
    assert "endpoint" in _http_error_hint(404)
    assert _http_error_hint(500) == ""


def test_http_403_error_message_carries_hint_without_secrets():
    """An HTTP 403 from the server surfaces the code, the server body and

    the actionable hint - and never a secret value.
    """
    import io
    from unittest.mock import patch
    from urllib.error import HTTPError

    client = McpHttpClient("https://example.test/mcp")
    fake_error = HTTPError(
        "https://example.test/mcp", 403, "Forbidden",
        {}, io.BytesIO(b"forbidden: access denied"),
    )
    with patch("urllib.request.urlopen", side_effect=fake_error):
        with pytest.raises(McpClientError) as exc:
            client._send_request("initialize", {}, timeout=5)
    message = str(exc.value)
    assert "HTTP 403" in message
    assert "forbidden: access denied" in message
    assert "fine-grained" in message
    assert "ghp_" in message
    assert "secret123" not in message
