# -*- coding: utf-8 -*-
"""
Scenario test: MCP preset -> draft -> credentials -> tool call (fake Vkusvill).

Walks the feature through its public entry points the way a user would use it:
  1. A bundled-style preset (an offline Vkusvill template whose URL points at
     a local fake HTTP MCP server) is listed and instantiated by DevAgent as
     a DRAFT connection (pending, memory seeded, no secrets).
  2. Test connection works on the tokenless draft; afterwards the USER fills
     only the credential (VKUSVILL_TOKEN) via ``env_pairs``. The connection
     becomes active and the cached HTTP client is dropped, so the next call
     rebuilds with the new environment.
  3. A private tool (order_history) then succeeds and the fake server proves
     the Authorization header really carried the resolved token; a public
     tool call is dispatched through the dynamic catalog name.
  4. Another draft without credentials can still use public tools, while a
     private tool reports a clean server-side auth error.

Runs fully offline: the MCP server is an in-process ``http.server`` on
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


class _FakeVkusvillServer:
    """Tiny HTTP MCP endpoint imitating the Vkusvill MCP surface."""

    session_id = "test-session-1"
    valid_token = "Bearer user-token"

    def __init__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output quiet
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except Exception:
                    body = {}
                outer.session_seen.append(
                    self.headers.get("Mcp-Session-Id") or "")
                outer.auth_seen.append(
                    self.headers.get("Authorization") or "")
                method = body.get("method")
                req_id = body.get("id")
                if method == "initialize":
                    self._reply(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "Vkusvill Fake",
                                           "version": "1.0"},
                        },
                    }, extra={"Mcp-Session-Id": outer.session_id})
                    return
                if method == "notifications/initialized":
                    self._reply_bytes(200, b"{}")
                    return
                if method == "tools/list":
                    self._reply(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"tools": [
                            {"name": "search_products",
                             "description": "Search products by query",
                             "inputSchema": {
                                 "type": "object",
                                 "properties": {
                                     "query": {"type": "string"},
                                 },
                                 "required": ["query"],
                             }},
                            {"name": "order_history",
                             "description": "Personal order history",
                             "inputSchema": {"type": "object"}},
                        ]},
                    })
                    return
                if method == "tools/call":
                    params = body.get("params") or {}
                    name = str(params.get("name") or "")
                    arguments = params.get("arguments") or {}
                    if name == "order_history":
                        auth = self.headers.get("Authorization") or ""
                        if auth.strip() != outer.valid_token:
                            result = {
                                "content": [{
                                    "type": "text",
                                    "text": "auth required: provide a "
                                            "valid VKUSVILL_TOKEN",
                                }],
                                "isError": True,
                            }
                        else:
                            result = {
                                "content": [{"type": "text",
                                             "text": "orders: [123, 456]"}],
                                "isError": False,
                            }
                    elif name == "search_products":
                        result = {
                            "content": [{
                                "type": "text",
                                "text": "products for: "
                                        + str(arguments.get("query", "")),
                            }],
                            "isError": False,
                        }
                    else:
                        self._reply(200, {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {"code": -32602,
                                      "message": f"unknown tool: {name}"},
                        })
                        return
                    self._reply(200, {"jsonrpc": "2.0", "id": req_id,
                                      "result": result})
                    return
                self._reply(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601,
                              "message": "method not found"},
                })

            def _reply(self, status, payload, extra=None):
                self._reply_bytes(
                    status, json.dumps(payload).encode("utf-8"),
                    extra=extra,
                )

            def _reply_bytes(self, status, body, extra=None):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        self.handler_cls = Handler
        self.session_seen = []
        self.auth_seen = []
        self.httpd = None
        self.thread = None

    def start(self) -> str:
        """Bind on an ephemeral localhost port; return the endpoint URL."""
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.handler_cls)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        port = self.httpd.server_address[1]
        return f"http://127.0.0.1:{port}/mcp"

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


@pytest.fixture()
def isolated_data_dir():
    """Point DATA_DIR and DB paths at a throwaway temp directory."""
    tmp = tempfile.mkdtemp(prefix="sagaai_mcp_preset_scenario_")
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

    from storage.db import reset_engine, reset_devagent_engine
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


def test_mcp_preset_draft_credentials_call_scenario(isolated_data_dir):
    """Full user path: preset -> draft -> token -> HTTP tool call."""
    from core import connector_presets
    from core import connectors
    import core.mcp_tools as mcp_tools

    fake = _FakeVkusvillServer()
    url = fake.start()
    try:
        # 1. An offline preset template (URL -> local fake server).
        presets_root = Path(isolated_data_dir) / "presets"
        (presets_root / "mcp").mkdir(parents=True)
        preset_file = presets_root / "mcp" / "vkusvill_local.json"
        preset_file.write_text(
            json.dumps({
                "name": "Vkusvill local",
                "icon": "🧪",
                "description": "Local fake of the Vkusvill MCP endpoint.",
                "sort_order": 10,
                "config": {
                    "transport": "http",
                    "url": url,
                    "auth_header": "Bearer {VKUSVILL_TOKEN}",
                    "secret_env_fields": {
                        "token": {
                            "env": "VKUSVILL_TOKEN",
                            "label": "Bearer token",
                            "help": "Unlocks personal data.",
                            "password": True,
                        },
                    },
                },
                "memory_seed": ("Local Vkusvill fake: public search works "
                                "without a token; VKUSVILL_TOKEN unlocks "
                                "order history. Do not store tokens here."),
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        listed_presets = connector_presets.list_presets(
            "mcp", preset_root=str(presets_root))
        assert [p["id"] for p in listed_presets] == ["vkusvill_local"]
        assert listed_presets[0]["icon"] == "🧪"

        # 2. DevAgent instantiates a draft (pending, memory seeded).
        made = connector_presets.instantiate_preset(
            "vkusvill_local", "mcp", name="Vkusvill local",
            preset_root=str(presets_root))
        conn = made["connection"]
        conn_id = conn["id"]
        assert conn["config"]["preset"] == "vkusvill_local"
        assert conn["config"]["transport"] == "http"
        assert conn["credentials_status"] == "pending"
        memory = connectors.get_connection_memory(conn_id)
        assert "VKUSVILL_TOKEN" in memory
        assert "Do not store tokens" in memory

        # 3. Test connection on the tokenless draft (initialize succeeds,
        #    public metadata is served without credentials).
        tested = mcp_tools.test_connection(conn_id)
        assert tested["ok"] is True, tested
        assert tested["server_name"] == "Vkusvill Fake v1.0"
        assert tested["tools_count"] == 2

        # 4. The USER fills only the credential; the connection activates
        #    and the cached client is dropped so the next call rebuilds.
        updated = connectors.update_connection(
            conn_id, secrets={"env_pairs": {"VKUSVILL_TOKEN": "user-token"}})
        assert updated["credentials_status"] == "active"

        # 5. The private tool works now that the token reached the client.
        private_ok = mcp_tools.mcp_call_tool(
            connector_id=conn_id, tool_name="order_history", arguments={})
        assert private_ok["ok"] is True
        assert private_ok["result"]["is_error"] is False
        assert private_ok["result"]["content"] == "orders: [123, 456]"

        # 6. The fake server saw the tokenless handshake FIRST, then the
        #    resolved Authorization header; the session id was replayed.
        assert "" in fake.auth_seen  # pre-token handshake had no header
        assert "Bearer user-token" in fake.auth_seen
        assert _FakeVkusvillServer.session_id in fake.session_seen

        # 7. The public tool goes through the dispatcher by dynamic name.
        from core.orchestrators import create_orchestrator
        from core.orchestrators import set_enabled_connections
        slug = "mcp_preset_e2e_orch"
        create_orchestrator(slug, "Mcp Preset E2E", "Test")
        assert set_enabled_connections(slug, [conn_id]) is True
        mcp_tools.clear_discovery_cache()

        from core.orchestrator_tools import build_tool_catalog
        dynamic_search = f"mcp__{conn_id}__search_products"
        catalog_names = [t["name"] for t in build_tool_catalog(slug)]
        assert dynamic_search in catalog_names

        from dev_agent.universal_agent import UniversalDevAgent
        agent = UniversalDevAgent()
        agent.attach_orchestrator(slug)
        result = agent.dispatch(dynamic_search, {"query": "кефир"})
        assert result["ok"] is True
        assert result["result"]["tool"] == "search_products"
        assert result["result"]["is_error"] is False
        assert "кефир" in result["result"]["content"]

        # 8. A second draft without credentials: public tools work, the
        #    private one yields a clean server-side auth error.
        made2 = connector_presets.instantiate_preset(
            "vkusvill_local", "mcp", name="Vkusvill public",
            preset_root=str(presets_root))
        conn2_id = made2["connection"]["id"]
        assert (connectors.get_connection(conn2_id)
                ["credentials_status"] == "pending")
        mcp_tools.clear_discovery_cache()

        public = mcp_tools.mcp_call_tool(
            connector_id=conn2_id, tool_name="search_products",
            arguments={"query": "молоко"})
        assert public["ok"] is True
        assert public["result"]["is_error"] is False
        assert "молоко" in public["result"]["content"]

        private = mcp_tools.mcp_call_tool(
            connector_id=conn2_id, tool_name="order_history", arguments={})
        assert private["ok"] is True
        assert private["result"]["is_error"] is True
        assert "VKUSVILL_TOKEN" in private["result"]["content"]
    finally:
        fake.stop()
