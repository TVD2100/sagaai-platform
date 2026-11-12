# -*- coding: utf-8 -*-
"""
Scenario: an MCP server from the catalog becomes a tile, not a connection.

Walks the path a user takes:

  1. The showcase shows an MCP server; its card states which credential the
     server needs and that installing it produces a tile.
  2. Installation stores the preset and creates NO connection - the
     Connections tab stays empty, which is the behaviour under test.
  3. The MCP servers tab lists the new tile next to the bundled ones.
  4. The user creates TWO connections from that one tile, each with its own
     token, and both work against the server - two accounts, one template.
  5. Uninstalling the showcase entry takes the tile away and leaves the
     user's connections alone: they are accounts, not files of the entity.

Runs fully offline: the MCP server is an in-process ``http.server`` bound to
127.0.0.1 (plain http is accepted for localhost only). The fake server
answers every tool call with the token it received, so the test proves that
each connection travels with its own credentials.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)

#: Catalog id of the server; the preset id matches it.
ENTITY_ID = "demo_mail_mcp"

#: Tokens of the two accounts the scenario creates from one tile.
TOKEN_ONE = "token-one"
TOKEN_TWO = "token-two"


class _FakeMailServer:
    """Minimal HTTP MCP endpoint that echoes the bearer token it received."""

    def __init__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output quiet
                pass

            def _token(self) -> str:
                header = self.headers.get("Authorization") or ""
                return header[len("Bearer "):] if header.startswith("Bearer ") else ""

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except Exception:
                    body = {}
                outer.tokens_seen.append(self._token())
                method = body.get("method")
                req_id = body.get("id")
                if method == "initialize":
                    self._reply(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "Demo Mail Fake",
                                           "version": "0.1"},
                        },
                    }, extra={"Mcp-Session-Id": outer.session_id})
                    return
                if method == "notifications/initialized":
                    self._reply_bytes(200, b"{}")
                    return
                if not self._token():
                    # Mirrors the real endpoint: no token, no tool surface.
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
                        "result": {"tools": [{
                            "name": "mail.whoami",
                            "description": "Return the token of this connection",
                            "inputSchema": {"type": "object"},
                        }]},
                    })
                    return
                if method == "tools/call":
                    params = body.get("params") or {}
                    if str(params.get("name") or "") == "mail.whoami":
                        result = {
                            "content": [{"type": "text",
                                         "text": f"account: {self._token()}"}],
                            "isError": False,
                        }
                    else:
                        result = {
                            "content": [{"type": "text",
                                         "text": "unknown tool"}],
                            "isError": True,
                        }
                    self._reply(200, {"jsonrpc": "2.0", "id": req_id,
                                      "result": result})
                    return
                self._reply(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": "method not found"},
                })

            def _reply(self, status, payload, extra=None):
                self._reply_bytes(status, json.dumps(payload).encode("utf-8"),
                                  extra=extra)

            def _reply_bytes(self, status, body, extra=None):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        self.handler_cls = Handler
        self.session_id = "demo-mail-session"
        self.tokens_seen = []
        self.httpd = None

    def start(self) -> str:
        """Bind on an ephemeral localhost port; return the endpoint URL."""
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.handler_cls)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


def _enable_marketplace(monkeypatch) -> None:
    """Turn the marketplace on for the scenario run."""
    from core.marketplace import config as marketplace_config

    values = {
        "enabled": True,
        "base_url": "",
        "allow_code": True,
        "require_signature": False,
        "channel": "main",
    }
    monkeypatch.setattr(
        marketplace_config, "marketplace_settings", lambda: dict(values)
    )


def _preset_block(url: str) -> dict:
    """Return the preset block the catalog publishes for the fake server."""
    return {
        "name": {"en": "Demo Mail", "ru": "Демо Почта", "zh-CN": "Demo Mail"},
        "description": {
            "en": "A fake mailbox server",
            "ru": "Тестовый почтовый сервер",
            "zh-CN": "A fake mailbox server",
        },
        "icon": "\U0001f4ec",
        "protocol_version": "2025-03-26",
        "secret_env_fields": {
            "token": {
                "env": "DEMO_MAIL_TOKEN",
                "label": "Mailbox token",
                "help": "Issued per mailbox.",
                "password": True,
            },
        },
        "config": {
            "transport": "http",
            "url": url,
            "auth_header": "Bearer {DEMO_MAIL_TOKEN}",
        },
        "memory_seed": "Fake mailbox server: one token per account.",
    }


def _catalog_view(tmp_path, serve_catalog, url: str):
    """Serve a one-server catalog and return the card of that server."""
    from core.marketplace import catalog
    from core.marketplace.source import MarketplaceSource

    entity, package = make_entity(
        entity_id=ENTITY_ID,
        version="1.0.0",
        type_="mcp",
        files={},
        extra={
            "preset": _preset_block(url),
            "preset_id": ENTITY_ID,
            "service": "mcp",
        },
    )
    root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(root)
    return catalog.fetch_card(
        ENTITY_ID, source=MarketplaceSource(base_url), type_="mcp"
    ), base_url


def test_a_catalog_mcp_server_becomes_a_tile_and_serves_many_connections(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    """Install a server, then create two independent accounts from its tile."""
    import core.mcp_tools as mcp_tools
    from core import connector_presets, connectors
    from core.marketplace import conflicts, installer, registry

    _enable_marketplace(monkeypatch)
    fake = _FakeMailServer()
    url = fake.start()
    try:
        # 1. The showcase card asks for the credential and says what an
        #    install produces.
        view, base_url = _catalog_view(tmp_path, serve_catalog, url)
        assert view["requires_secrets"] == ["DEMO_MAIL_TOKEN"]
        assert view["type"] == "mcp"

        # 2. Installation stores the preset and creates no connection.
        report = installer.install_entity(
            view["entity"], channel="main"
        )
        assert report["ok"] is True
        assert report["preset"] == ENTITY_ID
        assert report["origin"] == "user"
        assert connectors.list_connections() == []
        stored = (
            Path(marketplace_data_dir) / "connector_presets" / "mcp"
            / f"{ENTITY_ID}.json"
        )
        assert stored.is_file()
        assert json.loads(stored.read_text(encoding="utf-8"))["config"]["url"] == url
        assert registry.get_installed("mcp", ENTITY_ID)["runtime_slug"] == ENTITY_ID

        # 3. The tile is on the MCP servers tab, next to the bundled ones.
        tiles = connector_presets.list_presets("mcp")
        tile = next(t for t in tiles if t["id"] == ENTITY_ID)
        assert tile["origin"] == connector_presets.ORIGIN_USER
        assert tile["icon"] == "\U0001f4ec"
        assert "vkusvill" in [t["id"] for t in tiles]

        # 4. Two connections from the one tile, each with its own token.
        first = connector_presets.instantiate_preset(
            ENTITY_ID, "mcp", name="Mailbox one",
            secrets={"env_pairs": {"DEMO_MAIL_TOKEN": TOKEN_ONE}},
            pending=False,
        )["connection"]
        second = connector_presets.instantiate_preset(
            ENTITY_ID, "mcp", name="Mailbox two",
            secrets={"env_pairs": {"DEMO_MAIL_TOKEN": TOKEN_TWO}},
            pending=False,
        )["connection"]
        assert first["id"] != second["id"]
        assert first["config"]["preset"] == ENTITY_ID
        assert len(connectors.list_connections()) == 2
        mcp_tools.clear_discovery_cache()

        # 5. Both work, and each call carries its own account's token.
        assert mcp_tools.test_connection(first["id"])["ok"] is True
        assert mcp_tools.test_connection(second["id"])["ok"] is True
        mcp_tools.clear_discovery_cache()
        one = mcp_tools.mcp_call_tool(
            connector_id=first["id"], tool_name="mail.whoami", arguments={}
        )
        two = mcp_tools.mcp_call_tool(
            connector_id=second["id"], tool_name="mail.whoami", arguments={}
        )
        assert one["ok"] is True and one["result"]["is_error"] is False
        assert two["ok"] is True and two["result"]["is_error"] is False
        assert TOKEN_ONE in one["result"]["content"]
        assert TOKEN_TWO in two["result"]["content"]

        # 6. Removing the showcase entry takes the tile and keeps the
        #    accounts: they hold credentials the user typed.
        removed = conflicts.uninstall("mcp", ENTITY_ID)
        assert removed["preset_removed"] is True
        assert not stored.exists()
        assert ENTITY_ID not in [
            t["id"] for t in connector_presets.list_presets("mcp")]
        assert len(connectors.list_connections()) == 2
        assert connectors.get_connection(first["id"])
        assert connectors.get_connection(second["id"])
    finally:
        from core import mcp_client
        for key in list(getattr(mcp_client, "_HTTP_REGISTRY", {})):
            mcp_client.drop_client(key)
        for key in list(getattr(mcp_client, "_PROCESS_REGISTRY", {})):
            mcp_client.drop_client(key)
        fake.stop()
