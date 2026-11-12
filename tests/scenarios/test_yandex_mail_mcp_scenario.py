# -*- coding: utf-8 -*-
"""
Scenario test: Yandex 360 Mail MCP preset -> draft -> OAuth token -> tool call.

Walks the feature the way a user uses it:
  1. A bundled-style preset (the mail template whose URL points at a local
     fake HTTP MCP server) is listed and instantiated by DevAgent as a DRAFT
     connection: pending, memory seeded, no secrets.
  2. The mail server answers ``initialize`` without credentials but refuses
     ``tools/list`` with HTTP 401 until a real token arrives, so test
     connection on the tokenless draft fails with a clean auth error and
     without an ``Authorization`` header (not a bare ``OAuth `` prefix).
  3. The USER fills only the token through ``env_pairs``. The connection
     becomes active and the next call succeeds.
  4. The fake server proves the header carried the ``OAuth`` scheme - the
     endpoint rejects ``Bearer`` - and a mail tool call returns its payload.

Runs fully offline: the MCP server is an in-process ``http.server`` bound to
127.0.0.1 (plain http is accepted for localhost only).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import core.paths

#: Token the fake server accepts; the real one comes from the OAuth page.
VALID_TOKEN = "y0_AgAAAABfakeMailToken"


class _FakeYandexMailServer:
    """Minimal Yandex 360 mail MCP endpoint with OAuth-only authorization."""

    def __init__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output quiet
                pass

            def _authorized(self) -> bool:
                return (self.headers.get("Authorization") or "") == (
                    f"OAuth {VALID_TOKEN}"
                )

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except Exception:
                    body = {}
                outer.auth_seen.append(self.headers.get("Authorization") or "")
                method = body.get("method")
                req_id = body.get("id")
                if method == "initialize":
                    # The handshake is public: the server hides nothing here.
                    self._reply(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "Yandex Mail Fake",
                                           "version": "0.1"},
                        },
                    })
                    return
                if method == "notifications/initialized":
                    self._reply_bytes(200, b"{}")
                    return
                if not self._authorized():
                    # Mirrors the real endpoint: a missing or non-OAuth token
                    # answers 401 before the method is even looked at.
                    self._reply(401, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32001, "message": "Unauthorized"},
                    })
                    return
                if method == "tools/list":
                    self._reply(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"tools": [
                            {"name": "mail.list_folders",
                             "description": "Return mailbox folders",
                             "inputSchema": {"type": "object"}},
                            {"name": "mail.read_message",
                             "description": "Read a message body as HTML",
                             "inputSchema": {
                                 "type": "object",
                                 "properties": {
                                     "message_id": {"type": "string"},
                                 },
                                 "required": ["message_id"],
                             }},
                        ]},
                    })
                    return
                if method == "tools/call":
                    params = body.get("params") or {}
                    name = str(params.get("name") or "")
                    if name == "mail.list_folders":
                        result = {
                            "content": [{
                                "type": "text",
                                "text": json.dumps({"folders": [
                                    {"id": "inbox", "name": "Inbox"},
                                    {"id": "sent", "name": "Sent"},
                                ]}),
                            }],
                            "isError": False,
                        }
                    else:
                        result = {
                            "content": [{"type": "text",
                                         "text": f"unknown tool: {name}"}],
                            "isError": True,
                        }
                    self._reply(200, {"jsonrpc": "2.0", "id": req_id,
                                      "result": result})
                    return
                self._reply(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601,
                              "message": "method not found"},
                })

            def _reply(self, status, payload):
                self._reply_bytes(status,
                                  json.dumps(payload).encode("utf-8"))

            def _reply_bytes(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.handler_cls = Handler
        self.auth_seen = []
        self.httpd = None

    def start(self) -> str:
        """Bind on an ephemeral localhost port; return the endpoint URL."""
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.handler_cls)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1/mail/mcp"

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


@pytest.fixture()
def isolated_data_dir():
    """Point DATA_DIR and DB paths at a throwaway temp directory."""
    tmp = tempfile.mkdtemp(prefix="sagaai_yandex_mail_scenario_")
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    old_env_key = os.environ.get("SAGAAI_ENCRYPTION_KEY")
    os.environ["SAGAAI_DATA_DIR"] = tmp
    from cryptography.fernet import Fernet
    os.environ["SAGAAI_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

    old_attrs = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH",
                 "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        old_attrs[attr] = getattr(core.paths, attr, None)
    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    from storage.db import reset_devagent_engine, reset_engine
    reset_engine()
    reset_devagent_engine()

    import core.mcp_tools as mcp_tools
    mcp_tools.clear_discovery_cache()
    mcp_tools.MCP_DYNAMIC_REGISTRY.clear()

    yield tmp

    from core import mcp_client
    for key in list(getattr(mcp_client, "_PROCESS_REGISTRY", {})):
        mcp_client.drop_client(key)
    for key in list(getattr(mcp_client, "_HTTP_REGISTRY", {})):
        mcp_client.drop_client(key)
    reset_engine()
    reset_devagent_engine()

    if old_env:
        os.environ["SAGAAI_DATA_DIR"] = old_env
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)
    if old_env_key:
        os.environ["SAGAAI_ENCRYPTION_KEY"] = old_env_key
    else:
        os.environ.pop("SAGAAI_ENCRYPTION_KEY", None)
    for attr, value in old_attrs.items():
        if value is not None:
            setattr(core.paths, attr, value)
    shutil.rmtree(tmp, ignore_errors=True)


def _write_mail_preset(root: Path, url: str) -> Path:
    """Write the mail preset template pointed at the local fake server."""
    (root / "mcp").mkdir(parents=True, exist_ok=True)
    path = root / "mcp" / "yandex_mail_local.json"
    path.write_text(json.dumps({
        "name": "Яндекс Почта (локальный стенд)",
        "icon": "📬",
        "description": "Локальная копия MCP-сервера Яндекс Почты.",
        "sort_order": 30,
        "config": {
            "transport": "http",
            "url": url,
            "auth_header": "OAuth {YANDEX_MAIL_OAUTH_TOKEN}",
            "secret_env_fields": {
                "token": {
                    "env": "YANDEX_MAIL_OAUTH_TOKEN",
                    "label": "OAuth-токен Яндекс Почты",
                    "help": "Значение токена без слова OAuth.",
                    "password": True,
                },
            },
        },
        "memory_seed": ("Эндпоинт локального стенда. Отправка писем не "
                        "поддерживается; токен здесь не хранить."),
    }, ensure_ascii=False), encoding="utf-8")
    return path


def test_yandex_mail_preset_oauth_flow_scenario(isolated_data_dir):
    """Preset -> draft -> 401 without a token -> OAuth token -> tool call."""
    import core.mcp_tools as mcp_tools
    from core import connector_presets, connectors

    fake = _FakeYandexMailServer()
    url = fake.start()
    try:
        presets_root = Path(isolated_data_dir) / "presets"
        _write_mail_preset(presets_root, url)

        # 1. The preset is visible in the catalogue and instantiates a draft.
        listed = connector_presets.list_presets(
            "mcp", preset_root=str(presets_root))
        assert [p["id"] for p in listed] == ["yandex_mail_local"]
        made = connector_presets.instantiate_preset(
            "yandex_mail_local", "mcp", name="Яндекс Почта",
            preset_root=str(presets_root))
        conn = made["connection"]
        conn_id = conn["id"]
        assert conn["credentials_status"] == "pending"
        assert conn["config"]["auth_header"] == (
            "OAuth {YANDEX_MAIL_OAUTH_TOKEN}")
        memory = connectors.get_connection_memory(conn_id)
        assert "Отправка писем не поддерживается" in memory

        # 2. Without a token the handshake succeeds but the tool list is
        #    refused, and no Authorization header is sent at all.
        refused = mcp_tools.test_connection(conn_id)
        assert refused["ok"] is False
        assert "401" in refused["error"]
        assert "" in fake.auth_seen
        assert not any(
            value.startswith("OAuth ") for value in fake.auth_seen
        ), fake.auth_seen

        # 3. The user fills only the token; the connection becomes active.
        updated = connectors.update_connection(
            conn_id,
            secrets={"env_pairs": {"YANDEX_MAIL_OAUTH_TOKEN": VALID_TOKEN}},
        )
        assert updated["credentials_status"] == "active"
        mcp_tools.clear_discovery_cache()

        # 4. The same call now succeeds and the server sees the OAuth scheme.
        tested = mcp_tools.test_connection(conn_id)
        assert tested["ok"] is True, tested
        assert tested["tools_count"] == 2
        assert f"OAuth {VALID_TOKEN}" in fake.auth_seen
        assert not any(
            value.startswith("Bearer") for value in fake.auth_seen
        ), fake.auth_seen

        # 5. A mail tool call travels over the same authorized session.
        call = mcp_tools.mcp_call_tool(
            connector_id=conn_id, tool_name="mail.list_folders", arguments={})
        assert call["ok"] is True
        assert call["result"]["is_error"] is False
        payload = call["result"]["content"]
        assert "inbox" in payload and "Sent" in payload
    finally:
        fake.stop()
