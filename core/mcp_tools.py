# -*- coding: utf-8 -*-
"""
core.mcp_tools - orchestrator tool layer for the MCP connector (service mcp).

Exposes the v1 static tools ``mcp_list_tools`` / ``mcp_call_tool`` and builds
ranked dynamic per-server tools named ``mcp__<conn_id>__<tool_name>`` (ADR-001
rev. 2 p. 5) so a server's tools appear in the orchestrator catalog and can
be called directly by name from the dialog.

The layer mirrors ``core.github_tools_rest`` / ``core.ssh_tools``:

    invoke(**kwargs) -> dict

The first argument of every tool is ``connector_id`` (service ``mcp``).
Failures return ``{"ok": False, "error": ...}`` dicts so the agent loop feeds
them back to the model and the session never breaks (ADR-001 rev. 2 p. 6).

Security:

- ``env_json`` is decrypted only to build the environment of the server
  process; it is never logged and never appears in catalog entries or
  tool results.
- Tool names/descriptions coming from the server are untrusted data: they
  are sanitized (control characters stripped) and length-capped before
  entering the catalog, and never executed or interpreted.
- Workflow: errors are swallowed into ``{"ok": False, ...}`` dicts so the
  dispatcher can feed them back without crashing the platform; the agent
  sees the tool-call error and continues.

Catalog discovery is cached per connection for ``_DISCOVERY_TTL`` seconds:
``tools/list`` runs at most once per window and a failed start/list is
cached too, so a hanging or broken server never stalls page renders
(ADR-001 rev. 2 p. 6.4).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

__test__ = False  # pytest: these are tools, not unit-test functions

log = logging.getLogger("core.mcp_tools")

# ─── catalog discovery caching ──────────────────────────────────────────────
_DISCOVERY_TTL: float = 60.0
_MAX_DYNAMIC_TOOLS_PER_CONNECTION: int = 32
_MAX_DESC_LEN: int = 300
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_DYNAMIC_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")

_catalog_cache: Dict[str, Tuple[float, Optional[List[Dict[str, Any]]], str]] = {}
_cache_lock = threading.Lock()

# Reverse registry: dynamic tool name -> (conn_id, server-side tool name).
MCP_DYNAMIC_REGISTRY: Dict[str, Tuple[str, str]] = {}
_last_build_conns: Dict[str, Set[str]] = {}

_STATIC_CATALOG: Tuple[Dict[str, str], ...] = (
    {
        "name": "mcp_list_tools",
        "desc": (
            "List the tools of an MCP server connection (service 'mcp'). "
            "Arguments: connector_id (required)."
        ),
    },
    {
        "name": "mcp_call_tool",
        "desc": (
            "Call a tool of an MCP server connection by its server-side "
            "name. Arguments: connector_id (required), tool_name "
            "(required), arguments (optional dict or JSON string)."
        ),
    },
)


class McpToolError(ValueError):
    """User-facing error of the mcp tool layer."""


# ─── internal helpers ───────────────────────────────────────────────────────


def _get_conn_id(kwargs: Dict[str, Any]) -> str:
    """Extract and validate the connector_id argument."""
    conn_id = str(kwargs.get("connector_id") or "").strip()
    if not conn_id:
        raise McpToolError("Missing required argument: connector_id")
    return conn_id


def _enabled_mcp_conn_ids(slug: str) -> List[str]:
    """Return ids of enabled mcp connections of an orchestrator.

    Best effort: on any storage/decrypt error an empty list is returned, so
    catalog building can never break dispatching.
    """
    try:
        from core.orchestrators import get_enabled_connections
        from core.connectors import get_connection_full
        result: List[str] = []
        for conn_id in get_enabled_connections(slug):
            data = get_connection_full(str(conn_id))
            if isinstance(data, dict) and str(data.get("service") or "") == "mcp":
                result.append(str(conn_id))
        return result
    except Exception:
        return []


def _load_connection(conn_id: str) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Return (config, env) for an mcp connection.

    ``env_json`` is decrypted here and only here - the plaintext pairs are
    used exclusively to build the server process environment and are never
    logged or returned to the caller.
    """
    from core.connectors import get_connection_full, get_connection_secrets
    data = get_connection_full(conn_id)
    if not isinstance(data, dict):
        raise McpToolError(f"MCP connection not found: {conn_id}")
    if str(data.get("service") or "") != "mcp":
        raise McpToolError(f"Connection '{conn_id}' is not an MCP connection")
    config = data.get("config")
    if not isinstance(config, dict):
        raise McpToolError(f"MCP connection '{conn_id}' has no config")
    transport = str(config.get("transport") or "").strip().lower() or "stdio"
    if transport == "http":
        if not str(config.get("url") or ""):
            raise McpToolError(f"MCP connection '{conn_id}' has no url configured")
    elif not str(config.get("command") or ""):
        raise McpToolError(f"MCP connection '{conn_id}' has no command configured")
    env: Dict[str, str] = {}
    secrets_enc = data.get("secrets_encrypted")
    if isinstance(secrets_enc, dict) and secrets_enc.get("env_json"):
        raw = ""
        try:
            raw = get_connection_secrets(conn_id).get("env_json", "")
        except Exception:
            raw = ""
        raw = str(raw or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                raise McpToolError(
                    f"MCP connection '{conn_id}' env_json is invalid JSON"
                )
            if isinstance(parsed, dict):
                env = {str(k): str(v) for k, v in parsed.items()}
    return {str(k): v for k, v in config.items()}, env


def _client_for(conn_id: str):
    """Return a live client bound to the connection id (lazily started).

    stdio connections get a subprocess client, http connections get a URL
    client with the auth-header template resolved from the decrypted env.
    """
    config, env = _load_connection(conn_id)
    transport = str(config.get("transport") or "").strip().lower() or "stdio"
    if transport == "http":
        from core.mcp_client import McpClientError, get_http_client
        try:
            return get_http_client(
                conn_id,
                str(config["url"]),
                env,
                auth_header=str(config.get("auth_header") or ""),
            )
        except (McpClientError, OSError) as e:
            raise McpToolError(str(e))
    from core.mcp_client import McpClientError, get_client
    try:
        return get_client(
            conn_id,
            str(config["command"]),
            [str(a) for a in (config.get("args") or [])],
            env,
        )
    except (McpClientError, OSError) as e:
        raise McpToolError(str(e))


def _sanitize_desc(value: Any) -> str:
    """Neutralize untrusted server text: strip control chars, one line, cap."""
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = _CONTROL_CHARS_RE.sub(" ", text)
    return " ".join(text.split())[:_MAX_DESC_LEN]


def _dynamic_tool_name(conn_id: str, server_name: str) -> str:
    """Build the catalog name ``mcp__<conn_id>__<sanitized tool name>``."""
    sanitized = _DYNAMIC_TOOL_NAME_RE.sub("_", server_name.strip())[:64]
    sanitized = sanitized.strip("_") or "tool"
    return f"mcp__{conn_id}__{sanitized}"


def _normalize_tools(raw: Any) -> List[Dict[str, Any]]:
    """Normalize untrusted tools/list payloads into catalog-safe tool dicts.

    Non-dict entries, nameless entries and duplicate names are dropped;
    descriptions are sanitized; ``inputSchema`` goes through the client's
    schema translation so parameters use the platform catalog format.
    """
    from core.mcp_client import translate_schema_parameters
    tools: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    if not isinstance(raw, list):
        return tools
    for item in raw:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        server_name = raw_name.strip()
        key = _DYNAMIC_TOOL_NAME_RE.sub("_", server_name)[:64]
        clean = key.strip("_") or "tool"
        if clean in seen:
            continue
        seen.add(clean)
        try:
            parameters = translate_schema_parameters(item.get("inputSchema"))
        except Exception:
            parameters = []
        tools.append({
            "name": clean,
            "server_name": server_name,
            "description": _sanitize_desc(item.get("description")),
            "parameters": parameters if isinstance(parameters, list) else [],
        })
    return tools


def _discover_tools(conn_id: str) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """Return (tools, error) for the connection; cached for _DISCOVERY_TTL.

    The cache also stores failures: a hanging/broken server stalls the
    catalog at most once per TTL window.
    """
    now = time.monotonic()
    with _cache_lock:
        cached = _catalog_cache.get(conn_id)
        if cached and now - cached[0] < _DISCOVERY_TTL:
            cached_tools, cached_error = cached[1], cached[2]
            if cached_error:
                return None, cached_error
            return cached_tools, ""
    from core.mcp_client import McpClientError, McpTimeoutError
    try:
        client = _client_for(conn_id)
        data = client.list_tools()
    except McpTimeoutError:
        error = "MCP tools/list timed out; connection unavailable until recreated"
        with _cache_lock:
            _catalog_cache[conn_id] = (now, None, error)
        log.warning("MCP connection %s: %s; its tools are skipped", conn_id, error)
        return None, error
    except (McpClientError, McpToolError, OSError) as e:
        error = f"MCP tools/list failed: {e}"
        with _cache_lock:
            _catalog_cache[conn_id] = (now, None, error)
        log.warning("MCP connection %s: %s; its tools are skipped", conn_id, error)
        return None, error
    tools = _normalize_tools(data.get("tools") if isinstance(data, dict) else None)
    with _cache_lock:
        _catalog_cache[conn_id] = (time.monotonic(), tools, "")
    return tools, ""


def _describe_dynamic_tool(conn_id: str, tool: Dict[str, Any]) -> str:
    """Compose a catalog description for a dynamic server tool entry."""
    parts = [f"Tool '{tool['server_name']}' of MCP connection '{conn_id}'."]
    if tool["description"]:
        parts.append(tool["description"][:200])
    params = tool.get("parameters") or []
    if params:
        parts.append(
            "Arguments: "
            + ", ".join(
                f"{p['name']}:{p['type']}" + ("(required)" if p["required"] else "")
                for p in params[:12]
            )
        )
    return " ".join(parts)[:_MAX_DESC_LEN]


def _make_dynamic_fn(conn_id: str, server_name: str):
    """Return a dispatcher-compatible callable for one server tool."""

    def _invoke(**kwargs: Any) -> Dict[str, Any]:
        return _call_server_tool(conn_id, server_name, kwargs)

    _invoke.__name__ = f"mcp__{conn_id}__{server_name}"
    _invoke.__doc__ = f"MCP server tool '{server_name}' of connection '{conn_id}'."
    return _invoke


def _call_server_tool(conn_id: str, server_name: str,
                      arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one server tool; always returns an {'ok', ...} dict."""
    from core.mcp_client import McpClientError, McpTimeoutError
    try:
        client = _client_for(conn_id)
        result = client.call_tool(server_name, dict(arguments or {}))
    except McpTimeoutError as e:
        return {"ok": False, "error": f"MCP tool '{server_name}' timed out: {e}"}
    except (McpClientError, McpToolError, OSError) as e:
        return {"ok": False, "error": f"MCP tool '{server_name}' failed: {e}"}
    return {
        "ok": True,
        "result": {
            "tool": server_name,
            "content": str(result.get("content") or ""),
            "is_error": bool(result.get("is_error")),
        },
    }


# ─── static v1 tools ────────────────────────────────────────────────────────


def get_static_tools() -> list:
    """Return catalog metadata for the static MCP tools."""
    return [dict(t) for t in _STATIC_CATALOG]


def mcp_list_tools(**kwargs: Any) -> Dict[str, Any]:
    """List the tools of an MCP server connection.

    Arguments:
        connector_id (str, required): connection id.
    Returns:
        {"ok": True, "result": {"connection", "count", "tools": [
        {"name", "description", "parameters"}, ...]}}
    """
    try:
        conn_id = _get_conn_id(kwargs)
        tools, error = _discover_tools(conn_id)
        if tools is None:
            return {"ok": False, "error": error}
        return {
            "ok": True,
            "result": {
                "connection": conn_id,
                "count": len(tools),
                "tools": [
                    {
                        "name": t["server_name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    }
                    for t in tools
                ],
            },
        }
    except McpToolError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"MCP tool failed: {e}"}


def mcp_call_tool(**kwargs: Any) -> Dict[str, Any]:
    """Call a tool of an MCP server connection by its server-side name.

    Arguments:
        connector_id (str, required): connection id.
        tool_name (str, required): server-side tool name.
        arguments (dict | str, optional): tool arguments as a JSON object
            or a JSON string.
    Returns:
        {"ok": True, "result": {"tool", "content", "is_error"}}
    """
    try:
        conn_id = _get_conn_id(kwargs)
        tool_name = str(kwargs.get("tool_name") or "").strip()
        if not tool_name:
            raise McpToolError("Missing required argument: tool_name")
        arguments = kwargs.get("arguments")
        if isinstance(arguments, str):
            text = arguments.strip()
            if text:
                try:
                    arguments = json.loads(text)
                except (json.JSONDecodeError, TypeError, ValueError):
                    raise McpToolError("Argument 'arguments' is not valid JSON")
            else:
                arguments = {}
        if arguments in (None, ""):
            arguments = {}
        if not isinstance(arguments, dict):
            raise McpToolError("MCP tool arguments must be a JSON object")
        return _call_server_tool(conn_id, tool_name, arguments)
    except McpToolError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"MCP tool failed: {e}"}


def test_connection(conn_id: str) -> Dict[str, Any]:
    """Validate an mcp connection: start the server if needed and list tools.

    Reuses the live client of the connection (starting it once when the
    process is not running yet). Used by the Connectors page "Test
    connection" button. Returns ``{"ok": True, "server_name",
    "tools_count"}`` or ``{"ok": False, "error"}``.
    """
    conn_id = str(conn_id or "").strip()
    if not conn_id:
        return {"ok": False, "error": "Missing connection id"}
    try:
        client = _client_for(conn_id)
        info = client._cached_server_info
        name = str(info.get("name") or "") if isinstance(info, dict) else ""
        version = str(info.get("version") or "") if isinstance(info, dict) else ""
        tools, error = _discover_tools(conn_id)
        if tools is None:
            return {"ok": False, "error": error}
        label = name or conn_id
        if version:
            label = f"{label} v{version}"
        return {"ok": True, "server_name": label, "tools_count": len(tools)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── catalog assembly for the orchestrator layer ────────────────────────────


def collect_catalog_entries(
        orchestrator_slug: str, include_static: bool = True,
) -> List[Dict[str, str]]:
    """Return catalog entries for enabled mcp connections of a slug.

    Static v1 tools are emitted when requested; dynamic per-server tools
    are added for every connection whose ``tools/list`` succeeded. A
    connection whose server cannot start or list tools contributes nothing
    (a warning is logged instead - ADR-001 rev. 2 p. 6.2).
    """
    entries: List[Dict[str, str]] = []
    if include_static:
        entries.extend(get_static_tools())
    for conn_id in _enabled_mcp_conn_ids(orchestrator_slug):
        tools, _error = _discover_tools(conn_id)
        if tools is None:
            continue
        for tool in tools[:_MAX_DYNAMIC_TOOLS_PER_CONNECTION]:
            entries.append({
                "name": _dynamic_tool_name(conn_id, tool["server_name"]),
                "desc": _describe_dynamic_tool(conn_id, tool),
            })
    return entries


def build_dynamic_tools(orchestrator_slug: str) -> Dict[str, Any]:
    """Return {"mcp__<conn>__<tool>": callable} for enabled connections.

    The returned functions are dispatcher-compatible (``**kwargs``) and
    close over their connection id and server-side tool name. The reverse
    registry ``MCP_DYNAMIC_REGISTRY`` is kept in sync; stale entries of the
    slug's connections are pruned on every build.
    """
    fresh_names: List[str] = []
    conn_ids = _enabled_mcp_conn_ids(orchestrator_slug)
    result: Dict[str, Any] = {}
    for conn_id in conn_ids:
        tools, _error = _discover_tools(conn_id)
        if tools is None:
            continue
        for tool in tools[:_MAX_DYNAMIC_TOOLS_PER_CONNECTION]:
            name = _dynamic_tool_name(conn_id, tool["server_name"])
            fresh_names.append(name)
            MCP_DYNAMIC_REGISTRY[name] = (conn_id, tool["server_name"])
            result[name] = _make_dynamic_fn(conn_id, tool["server_name"])
    previous = _last_build_conns.get(orchestrator_slug, set())
    for name, (_conn_id, _svc) in list(MCP_DYNAMIC_REGISTRY.items()):
        if _conn_id in previous and name not in fresh_names:
            MCP_DYNAMIC_REGISTRY.pop(name, None)
    _last_build_conns[orchestrator_slug] = set(conn_ids)
    return result


def clear_discovery_cache() -> None:
    """Drop cached catalog discoveries (tests / diagnostics)."""
    with _cache_lock:
        _catalog_cache.clear()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
