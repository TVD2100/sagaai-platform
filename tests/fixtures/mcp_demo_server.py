# -*- coding: utf-8 -*-
"""tests.fixtures.mcp_demo_server - stdlib-only demo MCP server.

A tiny JSON-RPC 2.0 server speaking the MCP tools protocol (2025-03-26)
over stdio: one JSON message per line on stdin/stdout, UTF-8. Used as the
server process of the MCP client tests and the e2e demo.

Tools:
  - echo: returns the text argument back ("echo:<text>").
  - fail: always returns isError=True (tool-level failure).
  - image: returns a non-text image content block (must be rejected).
  - slow: sleeps 120 seconds (client timeout scenarios).

Optional argv:
  argv[1] - state file (JSON): every tools/list call bumps {"list": n}.
  argv[2] - protocol version override for initialize (version tests).
"""
from __future__ import annotations

import json
import os
import sys
import time

PROTOCOL_VERSION = "2025-03-26"

STATE_FILE = sys.argv[1] if len(sys.argv) > 1 else ""
PROTO = sys.argv[2] if len(sys.argv) > 2 else PROTOCOL_VERSION

TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {"name": "fail", "description": "Always fails.",
     "inputSchema": {"type": "object"}},
    {"name": "image", "description": "Returns a non-text content block.",
     "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "Sleeps past any client timeout.",
     "inputSchema": {"type": "object"}},
]


def send(obj: dict) -> None:
    """Write one JSON-RPC message line to stdout."""
    print(json.dumps(obj), flush=True)


def bump(key: str) -> None:
    """Increment a counter in the optional state file (best effort)."""
    if not STATE_FILE:
        return
    try:
        data: dict = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data[key] = int(data.get(key, 0)) + 1
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except (OSError, ValueError):
        pass


def main() -> None:
    print("mcp-demo-server started", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method", "")
        rid = msg.get("id")
        if method == "initialize":
            send({
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "protocolVersion": PROTO,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "demo", "version": "1"},
                },
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            bump("list")
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments") or {}
            if name == "echo":
                send({
                    "jsonrpc": "2.0", "id": rid,
                    "result": {
                        "content": [
                            {"type": "text",
                             "text": "echo:" + str(args.get("text", ""))}
                        ],
                        "isError": False,
                    },
                })
            elif name == "fail":
                send({
                    "jsonrpc": "2.0", "id": rid,
                    "result": {
                        "content": [{"type": "text", "text": "boom"}],
                        "isError": True,
                    },
                })
            elif name == "image":
                send({
                    "jsonrpc": "2.0", "id": rid,
                    "result": {
                        "content": [
                            {"type": "image", "data": "x",
                             "mimeType": "image/png"}
                        ],
                    },
                })
            elif name == "slow":
                time.sleep(120)
            else:
                send({
                    "jsonrpc": "2.0", "id": rid,
                    "error": {
                        "code": -32602, "message": "unknown tool",
                        "data": {"name": name},
                    },
                })
        else:
            send({
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "method not found"},
            })


if __name__ == "__main__":
    main()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
