# -*- coding: utf-8 -*-
"""
core.mcp_client - minimal JSON-RPC 2.0 client for MCP servers over stdio.

A self-contained MCP client (specification 2025-03-26, tools-only) built
with the standard library only - no JSON-RPC, no pydantic, no MCP SDK. The
server is started as a local subprocess (``shell=False``, never shell); the
transport is line-delimited JSON over stdio, UTF-8, one message per line.

Lifecycle per connection:

- ``start()`` launches the process and performs the initialize handshake
  (``initialize`` request with protocolVersion and client capabilities,
  followed by the ``notifications/initialized`` notification). A server that
  does not speak the supported protocol version fails the start.
- ``list_tools(timeout=30)`` asks ``tools/list`` and caches the result for
  the lifetime of the process.
- ``call_tool(name, arguments, timeout=60)`` performs ``tools/call``. Only
  textual content blocks are accepted; any image/audio/resource block makes
  the call fail with a clear error.
- On any timeout the process is terminated and marked dead. A dead client
  may not be reused and must be recreated; automatic retries are forbidden.

Processes are tracked in a registry keyed by connection id: creating a
second client for the same connection kills the previous process first, so
server processes can never accumulate. A crashing server never affects the
platform.

The schema translation turns an ``inputSchema`` (JSON Schema 2020-12 as
produced by MCP servers) into the platform tool-catalog parameter format.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


PROTOCOL_VERSION: str = "2025-03-26"

# ─── hard timeouts ──────────────────────────────────────────────────────────
LIST_TOOLS_TIMEOUT: float = 30.0
CALL_TOOL_TIMEOUT: float = 60.0

_STDERR_LIMIT: int = 2000
_RESPONSE_SIZE_LIMIT: int = 8 * 1024 * 1024
_AUTH_HDR_PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")

log = logging.getLogger("core.mcp_client")

# Alive clients keyed by (conn_id, server_key). Only one server process per
# connection may live; creating a replacement kills the previous one.
_PROCESS_REGISTRY: Dict[str, "McpClient"] = {}
# Reentrant: _register_self/_get_client call close()/unregister while
# already holding the registry lock (replacement semantics).
_PROCESS_REGISTRY_LOCK = threading.RLock()


# ─── errors ─────────────────────────────────────────────────────────────────


class McpClientError(Exception):
    """Base error of the MCP client layer (user-facing message)."""


class McpServerStartError(McpClientError):
    """The server process could not be started or the handshake failed."""


class McpTimeoutError(McpClientError):
    """The server did not answer within the hard timeout; the process was
    terminated and the client is dead."""


class McpProtocolError(McpClientError):
    """The server sent an invalid JSON-RPC response."""


# ─── schema translation (inputSchema -> platform parameters) ────────────────


def _type_dialect(schema: Optional[Dict[str, Any]]) -> str:
    """Return the effective JSON-Schema dialect of a schema."""
    root = schema if isinstance(schema, dict) else {}
    for key in ("$schema", "dialect"):
        dialect = str(root.get(key) or "").strip()
        if dialect:
            return dialect
    return ""


def _expected_types(schema: Optional[Dict[str, Any]]) -> List[str]:
    """Return the JSON type names a schema allows.

    Handles both the 2024-12 vocabulary (``type`` as an array) and the
    classic single-string form so servers using either dialect keep working.
    """
    node = schema if isinstance(schema, dict) else {}
    raw = node.get("type")
    if isinstance(raw, list):
        types: List[str] = []
        for item in raw:
            if isinstance(item, str):
                types.append(item)
        return types
    if isinstance(raw, str):
        return [raw]
    return []


def _has_control_chars(text: str) -> bool:
    """Return True when the string contains control characters."""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def translate_schema_parameters(schema: Optional[Dict[str, Any]],
                                required: Optional[List[Any]] = None) \
        -> List[Dict[str, Any]]:
    """Translate an MCP ``inputSchema`` into catalog parameter dicts.

    The ``required`` property names are read from the schema itself (JSON
    Schema 2020-12 keeps them inside the object) unless *required* is given
    explicitly. Returns up to 12 entries shaped like the platform tool
    catalog: ``{"name", "type", "required", "description"}``. Unknown
    properties are skipped best-effort; schema data is treated as untrusted
    input and never evaluated.
    """
    node = schema if isinstance(schema, dict) else {}
    req_raw = (
        required if isinstance(required, list) else node.get("required")
    )
    required_names = [str(n) for n in (req_raw or []) if isinstance(n, str)]
    props = node.get("properties")
    if not isinstance(props, dict) or not props:
        return []
    params: List[Dict[str, Any]] = []
    for name, prop in list(props.items())[:12]:
        if not isinstance(name, str):
            continue
        name = name.strip()
        # Untrusted server data: control characters or unreasonable length
        # must never reach the tool catalog - skip such parameter names.
        if not name or len(name) > 64 or _has_control_chars(name):
            continue
        prop = prop if isinstance(prop, dict) else {}
        types = _expected_types(prop)
        ptype = types[0] if types else "string"
        if ptype == "integer":
            ptype = "number"
        elif ptype not in ("number", "string", "boolean", "array", "object"):
            ptype = "string"
        desc = " ".join(str(prop.get("description") or "").split())[:200]
        params.append({
            "name": name,
            "type": ptype,
            "required": name in required_names,
            "description": desc,
        })
    return params


# ─── JSON-RPC transport helpers ─────────────────────────────────────────────


def _parse_line(line: bytes) -> Dict[str, Any]:
    """Parse one JSON-RPC message line (untrusted data, size-capped)."""
    if len(line) > _RESPONSE_SIZE_LIMIT:
        raise McpProtocolError(
            f"MCP server sent an oversized message ({len(line)} bytes)"
        )
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise McpProtocolError(f"MCP server sent invalid JSON: {e}")
    if not isinstance(data, dict):
        raise McpProtocolError("MCP server sent a non-object message")
    return data


def _parse_sse_messages(raw: bytes, request_id: Optional[int]) -> Dict[str, Any]:
    """Return the JSON-RPC message for *request_id* from an SSE payload.

    Every ``data:`` line is parsed as one message; server notifications
    inside the stream are skipped. A stream without the requested response
    raises a protocol error (untrusted data, size-capped per line).
    """
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].lstrip()
        if not data.strip():
            continue
        try:
            message = _parse_line(data.encode("utf-8"))
        except McpProtocolError:
            continue
        if message.get("method"):
            continue
        if message.get("id") == request_id:
            return message
    raise McpProtocolError(
        "MCP SSE response contains no message for the request"
    )


def _resolve_http_auth_header(template: str,
                              env: Dict[str, str]) -> Optional[str]:
    """Resolve an auth-header template to a concrete header line.

    Templates are stored WITHOUT secrets: placeholders like ``{TOKEN}`` are
    substituted from the decrypted ``env`` pairs at request time. A template
    that already looks like a full header (``Name: value``) is returned
    as-is; anything else is treated as the Authorization header value. The
    resolved secret is never logged.
    """
    if not template:
        return None
    placeholders = _AUTH_HDR_PLACEHOLDER_RE.findall(template or "")
    if placeholders and all(
        not str(env.get(name, "") or "").strip() for name in placeholders
    ):
        # Every secret referenced by the template is empty (e.g. an optional
        # token): a bare "Bearer " header would be meaningless - skip it.
        return None
    resolved = _AUTH_HDR_PLACEHOLDER_RE.sub(
        lambda m: str(env.get(m.group(1), "")), template
    )
    if not resolved.strip():
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in resolved):
        raise McpClientError("MCP auth_header resolved to an invalid header")
    if re.match(r"^[A-Za-z0-9-]+:", resolved):
        return resolved
    return f"Authorization: {resolved}"


def _http_error_hint(code: int) -> str:
    """Return a short, actionable hint for common HTTP auth failures.

    The hint names only facts about the failed request (status code), never
    secret values. 403 includes the GitHub MCP specifics: the hosted
    endpoint does not accept classic PATs (ghp_...) and gates the /mcp/
    surface behind account-level access.
    """
    if code == 401:
        return (
            "credentials are missing, expired or lack the required "
            "scopes - update them on the Connectors page"
        )
    if code == 403:
        return (
            "credentials are valid but not authorized for this endpoint "
            "(GitHub MCP accepts only fine-grained PATs "
            "github_pat_... with repo read permissions; classic ghp_... "
            "tokens are rejected and the account must have access to "
            "the GitHub Copilot endpoints)"
        )
    if code == 429:
        return "the server rate-limited the request - retry later"
    if code == 404:
        return "the endpoint was not found - check the configured URL"
    return ""


class McpHttpClient:
    """HTTP transport MCP client (JSON-RPC over HTTP POST + SSE).

    Streamable HTTP flavour: the initialize handshake and every tools/*
    request are sent as HTTP POSTs (urllib, stdlib only). Responses may be
    a plain JSON body or an SSE stream (``text/event-stream``); the
    ``Mcp-Session-Id`` header echoed by the server is replayed on all
    follow-up requests. An auth-header template given at construction time
    is resolved from the decrypted env pairs per request and is never
    logged.
    """

    def __init__(self, url: str, env: Optional[Dict[str, str]] = None,
                 auth_header: Optional[str] = None,
                 server_key: Optional[str] = None,
                 timeout: float = LIST_TOOLS_TIMEOUT):
        self.url = url
        self.env: Dict[str, str] = {
            str(k): str(v) for k, v in (env or {}).items()
        }
        self.auth_header = (auth_header or "").strip()
        self.server_key = server_key or url
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._tools_cache: Optional[Dict[str, Any]] = None
        self._cached_server_info: Optional[Dict[str, Any]] = None
        self._next_id: int = 1
        self._id_lock = threading.Lock()
        self._call_lock = threading.RLock()
        self.started: bool = False
        self.dead: bool = False

    def _new_id(self) -> int:
        with self._id_lock:
            current = self._next_id
            self._next_id += 1
            return current

    def _build_headers(self) -> Dict[str, str]:
        """Return the request headers incl. the resolved auth header."""
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "SagaAI-MCP/1.0",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        resolved = _resolve_http_auth_header(self.auth_header, self.env)
        if resolved:
            name, _, value = resolved.partition(":")
            headers[name.strip()] = value.strip()
        return headers

    def _capture_session(self, req: urllib.request.Request) -> None:
        """Remember the Mcp-Session-Id of a response (best effort)."""
        headers = getattr(req, "headers", None)
        if not headers:
            return
        value = None
        try:
            value = headers.get("Mcp-Session-Id") or headers.get(
                "mcp-session-id")
        except (AttributeError, TypeError):
            value = None
        if value:
            self.session_id = str(value).strip()[:200]

    def _read_response(self, response: Any,
                       request_id: Optional[int]) -> Dict[str, Any]:
        """Decode a plain-JSON or SSE HTTP response into a JSON-RPC message."""
        headers = getattr(response, "headers", None)
        raw = response.read()
        content_type = ""
        try:
            content_type = str(headers.get("Content-Type") or "")
        except (AttributeError, TypeError):
            content_type = ""
        try:
            session_value = headers.get("Mcp-Session-Id")
            if session_value:
                self.session_id = str(session_value).strip()[:200]
        except (AttributeError, TypeError):
            pass
        if len(raw) > _RESPONSE_SIZE_LIMIT:
            raise McpProtocolError(
                "MCP server sent an oversized response "
                f"({len(raw)} bytes)"
            )
        if "text/event-stream" in content_type.lower():
            return _parse_sse_messages(raw, request_id)
        return _parse_line(raw)

    def _send_request(self, method: str, params: Any,
                      timeout: float, has_id: bool = True) -> Dict[str, Any]:
        """POST one JSON-RPC request and return the decoded response.

        The body is a plain JSON object or an SSE stream; both are parsed
        with the same untrusted-data guards as the stdio transport.
        """
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method,
                                  "params": params}
        request_id: Optional[int] = None
        if has_id:
            request_id = self._new_id()
            payload["id"] = request_id
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._build_headers()
        req = urllib.request.Request(
            self.url, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                message = self._read_response(resp, request_id)
        except urllib.error.HTTPError as e:
            snippet = ""
            try:
                raw_err = e.read()
                snippet = raw_err.decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            message = f"MCP server responded HTTP {e.code}"
            if snippet.strip():
                message += f": {snippet.strip()}"
            hint = _http_error_hint(e.code)
            if hint:
                message += f" - hint: {hint}"
            raise McpClientError(message)
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            raise McpClientError(f"MCP request failed: {reason}")
        except socket.timeout:
            raise McpClientError(
                f"MCP server did not answer within {timeout:g}s"
            )
        if message.get("method"):
            raise McpProtocolError(
                "MCP server sent an unexpected notification"
            )
        if request_id is not None and message.get("id") != request_id:
            raise McpProtocolError(
                f"MCP message id mismatch: got {message.get('id')!r}, "
                f"expected {request_id!r}"
            )
        return message

    def _request(self, method: str, params: Any,
                 timeout: float) -> Dict[str, Any]:
        """Send a JSON-RPC request and wait for its response (HTTP)."""
        return self._send_request(method, params, timeout)

    def _notify(self, method: str, params: Any) -> None:
        """Send a JSON-RPC notification (fire-and-forget, best effort)."""
        try:
            self._send_request(method, params, self.timeout, has_id=False)
        except McpClientError:
            pass  # notifications are best effort by design

    def _initialize(self, timeout: float) -> None:
        """Perform the initialize handshake (initialize + initialized).

        Mirrors the stdio client: the pinned protocol version and the tools
        capability are checked; the server info is cached for test_connection.
        """
        response = self._request(
            "initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "SagaAI", "version": "1.0.0"},
            },
            timeout=timeout,
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpServerStartError(
                "MCP server returned an invalid initialize result"
            )
        protocol = str(result.get("protocolVersion") or "").strip()
        if protocol != PROTOCOL_VERSION:
            raise McpServerStartError(
                f"MCP protocol version mismatch: server speaks "
                f"'{protocol or 'unknown'}', expected '{PROTOCOL_VERSION}'"
            )
        caps = result.get("capabilities")
        if not isinstance(caps, dict) or "tools" not in caps:
            raise McpServerStartError(
                "MCP server does not advertise tools support"
            )
        self._cached_server_info = result.get("serverInfo")
        self._notify("notifications/initialized", {})

    def start(self, handshake_timeout: float = 30.0) -> None:
        """Perform the initialize handshake over HTTP.

        Raises:
            McpServerStartError: unreachable endpoint, HTTP error, or a
                server that does not speak the pinned protocol version.
        """
        if not self.url:
            raise McpServerStartError("MCP http url cannot be empty")
        try:
            self._initialize(handshake_timeout)
        except McpClientError as e:
            self.dead = True
            raise McpServerStartError(str(e))
        self.started = True

    def _ensure_started(self) -> None:
        if not self.started:
            raise McpClientError("MCP server is not started")
        if self.dead:
            raise McpClientError(
                "MCP connection is unavailable; recreate the connection "
                "(no automatic retry)"
            )

    def list_tools(self, timeout: float = LIST_TOOLS_TIMEOUT) -> Dict[str, Any]:
        """Return the raw result of tools/list (cached per session)."""
        self._ensure_started()
        if self._tools_cache is not None:
            return self._tools_cache
        with self._call_lock:
            if self._tools_cache is not None:
                return self._tools_cache
            response = self._request("tools/list", {}, timeout=timeout)
            result = response.get("result")
            if "error" in response:
                raise McpClientError(
                    f"tools/list failed: {_format_error(response['error'])}"
                )
            if not isinstance(result, dict) or not isinstance(
                    result.get("tools"), list):
                raise McpProtocolError(
                    "MCP tools/list returned an invalid result"
                )
            self._tools_cache = result
            return result

    def call_tool(self, tool_name: str,
                  arguments: Optional[Dict[str, Any]] = None,
                  timeout: float = CALL_TOOL_TIMEOUT,
                  ) -> Dict[str, Any]:
        """Call a tool; returns content text and the is_error flag.

        Same content rules as the stdio client: text blocks only, any
        non-text block fails the call with a clear error.
        """
        name = str(tool_name or "").strip()
        if not name:
            raise McpClientError("Missing required argument: tool_name")
        self._ensure_started()
        params: Dict[str, Any] = {"name": name}
        if arguments is not None:
            if not isinstance(arguments, dict):
                raise McpClientError(
                    "MCP tool arguments must be a JSON object"
                )
            params["arguments"] = arguments
        with self._call_lock:
            response = self._request("tools/call", params, timeout=timeout)
        if "error" in response:
            raise McpClientError(
                f"tools/call failed: {_format_error(response['error'])}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError("MCP tools/call returned an invalid result")
        is_error = bool(result.get("isError", False))
        content = result.get("content")
        if not isinstance(content, list):
            raise McpProtocolError(
                "MCP tools/call result has no content list"
            )
        text_parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                text_parts.append(str(block))
                continue
            if block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
            else:
                raise McpClientError(
                    "MCP tool returned non-text content "
                    f"(type: {block.get('type')!r}); non-text results "
                    "are not supported"
                )
        separator = chr(10) * 2
        return {"content": separator.join(text_parts), "is_error": is_error}

    def close(self) -> None:
        """Close the HTTP session (idempotent, no network involved)."""
        self.dead = True
        self.started = False


class McpClient:
    """One stdio session with an MCP server.

    Instantiate, then ``start()`` (handshake), then ``list_tools()`` /
    ``call_tool()`` / ``close()``. The class keeps one subprocess and the
    tools cache between calls; a dead process invalidates the cache and all
    further calls fail until the client is recreated.

    Args:
        command: executable basename (from the connector whitelist); the
            full path is resolved via ``shutil.which`` - shell is never
            used.
        args: list of string arguments passed to the executable.
        env: explicit environment pairs for the server process. The
            platform merges them into a clean inherited environment; it
            NEVER inherits secret environment variables of the platform
            itself (only explicit user-provided pairs are allowed).
        server_key: registry key of this client (connection id); one live
            process per key, creating a new client kills the previous one.
        stderr_handler: callable ``(str)`` receiving server stderr lines,
            used for logging only - stderr content is never parsed.
    """

    def __init__(self, command: str, args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None,
                 server_key: Optional[str] = None,
                 stderr_handler=None):
        self.command = command
        self.args: List[str] = [str(a) for a in (args or [])]
        self.env: Dict[str, str] = {
            str(k): str(v) for k, v in (env or {}).items()
        }
        self.server_key = server_key or command
        self.stderr_handler = stderr_handler
        self.process: Optional[subprocess.Popen] = None
        self.stdin = None
        self.stdout = None
        self._stderr_drain: Optional[threading.Thread] = None
        self._stdout_drain: Optional[threading.Thread] = None
        self._response_queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._tools_cache: Optional[Dict[str, Any]] = None
        self._cached_server_info: Optional[Dict[str, Any]] = None
        self._next_id: int = 1
        self._id_lock = threading.Lock()
        self._call_lock = threading.RLock()
        self.started: bool = False
        self.dead: bool = False

    def _resolve_executable(self) -> str:
        """Resolve the server command to an executable path.

        Absolute paths are allowed (validated when the connector was
        saved) when they exist and are executable; bare names go through
        PATH. The resolved string is passed to ``subprocess.Popen`` as-is
        with ``shell=False`` - shell metacharacters never apply.
        """
        if os.path.isabs(self.command):
            if os.path.isfile(self.command) and os.access(
                    self.command, os.X_OK):
                return self.command
            return ""
        import shutil
        return shutil.which(self.command) or ""

    def _server_env(self) -> Dict[str, str]:
        """Build the environment for the server process.

        Only a neutral, non-secret subset of the platform environment is
        inherited (PATH, HOME, temp/system dirs for interpreter caches)
        plus the explicit pairs from the connector env_json. Platform
        secrets never leak into the MCP server process.
        """
        neutral_keys = ("PATH", "HOME", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP")
        env: Dict[str, str] = {
            key: os.environ[key] for key in neutral_keys if key in os.environ
        }
        env.update(self.env)
        return env

    # ── process lifecycle ──────────────────────────────────────────────

    def start(self, handshake_timeout: float = 30.0) -> None:
        """Launch the server process and perform the initialize handshake.

        Raises:
            McpServerStartError: command missing/bad, process died during
                the handshake, or the server does not support the pinned
                protocol version.
        """
        executable = self._resolve_executable()
        if not executable:
            raise McpServerStartError(
                f"MCP command not found on this system: {self.command}"
            )
        cmdline: List[str] = [executable] + self.args
        try:
            self.process = subprocess.Popen(
                cmdline,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=self._server_env(),
            )
        except Exception as e:
            raise McpServerStartError(f"Cannot start MCP server: {e}")
        self.stdin = self.process.stdin
        self.stdout = self.process.stdout
        self._stderr_drain = threading.Thread(
            target=self._drain_stderr, name=f"mcp-stderr-{self.server_key}",
            daemon=True,
        )
        self._stderr_drain.start()
        self._start_stdout_drain()
        self._register_self()
        try:
            self._initialize(handshake_timeout)
        except McpClientError as e:
            self.close()
            raise e
        self.started = True

    def _register_self(self) -> None:
        """Register this client in the process registry; kill the previous
        process of the same server_key."""
        with _PROCESS_REGISTRY_LOCK:
            previous = _PROCESS_REGISTRY.get(self.server_key)
            if previous and previous is not self:
                previous.close()
            _PROCESS_REGISTRY[self.server_key] = self

    def _unregister_self(self) -> None:
        with _PROCESS_REGISTRY_LOCK:
            current = _PROCESS_REGISTRY.get(self.server_key)
            if current is self:
                _PROCESS_REGISTRY.pop(self.server_key, None)

    def _start_stdout_drain(self) -> None:
        """Start the background reader feeding response lines into the queue.

        A dedicated reader thread makes timeouts interruptible on every
        platform (pipes have no reliable select support on Windows)."""
        self._stdout_drain = threading.Thread(
            target=self._drain_stdout, name=f"mcp-stdout-{self.server_key}",
            daemon=True,
        )
        self._stdout_drain.start()

    def _drain_stdout(self) -> None:
        """Read server stdout lines into the response queue.

        A sentinel ``None`` marks EOF/process death. Lines are raw bytes - the
        size-capped, untrusted-data parsing happens in ``_parse_line``."""
        proc = self.process
        if proc is None or proc.stdout is None:
            self._response_queue.put(None)
            return
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                self._response_queue.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._response_queue.put(None)

    def _drain_stderr(self) -> None:
        """Read server stderr lines and forward them to the logger/handler.

        The stderr content is data for the log only - it is never parsed or
        trusted."""
        proc = self.process
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text and len(text) > _STDERR_LIMIT:
                    text = text[:_STDERR_LIMIT] + "..."
                if self.stderr_handler is not None:
                    try:
                        self.stderr_handler(text)
                    except Exception:
                        pass
                else:
                    log.warning("mcp[%s] stderr: %s", self.server_key, text)
        except (OSError, ValueError):
            pass  # pipe closed with the process

    def _initialize(self, timeout: float) -> None:
        """Perform the initialize handshake (initialize + initialized).

        Sends ``initialize`` with the pinned protocol version and client
        capabilities, then the ``notifications/initialized`` notification.
        A server that does NOT understand this protocol version is rejected
        as a start error.
        """
        response = self._request(
            "initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "SagaAI", "version": "1.0.0"},
            },
            timeout=timeout,
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpServerStartError(
                "MCP server returned an invalid initialize result"
            )
        protocol = str(result.get("protocolVersion") or "").strip()
        if protocol != PROTOCOL_VERSION:
            raise McpServerStartError(
                f"MCP protocol version mismatch: started server speaks "
                f"'{protocol or 'unknown'}', expected '{PROTOCOL_VERSION}'"
            )
        caps = result.get("capabilities")
        if not isinstance(caps, dict) or "tools" not in caps:
            raise McpServerStartError(
                "MCP server does not advertise tools support"
            )
        self._cached_server_info = result.get("serverInfo")
        self._notify("notifications/initialized", {})

    # ── low-level JSON-RPC ──────────────────────────────────────────────

    def _new_id(self) -> int:
        with self._id_lock:
            current = self._next_id
            self._next_id += 1
            return current

    def _send(self, payload: Dict[str, Any]) -> None:
        """Write one JSON message line to the server stdin."""
        if self.stdin is None:
            raise McpClientError("MCP client is not started")
        line = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.stdin.write(line + b"\n")
            self.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise McpClientError(
                f"Cannot write to MCP server process: {e}"
            )

    def _notify(self, method: str, params: Any) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: Any,
                 timeout: float) -> Dict[str, Any]:
        """Send a JSON-RPC request and wait for its response.

        The wire format enforces ``jsonrpc: 2.0`` and id match. A timeout
        terminates the process and marks the client dead (processes never
        linger). Server notifications received in between are ignored; an
        error object raises... (validated by the caller).
        """
        request_id = self._new_id()
        self._send({
            "jsonrpc": "2.0", "id": request_id, "method": method,
            "params": params,
        })
        poll_interval = 0.02
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._ensure_alive()
            remaining = deadline - time.monotonic()
            line = self._read_response_line(min(remaining, 5.0))
            if line is None:
                continue
            message = _parse_line(line)
            if message.get("method"):
                continue  # server notification, not our response
            rid = message.get("id")
            if rid is None:
                # a response to another request (shouldn't happen, the
                # transport is synchronous) - ignore to stay tolerant.
                continue
            if rid != request_id:
                raise McpProtocolError(
                    f"MCP message id mismatch: got {rid!r}, expected "
                    f"{request_id!r}"
                )
            if message.get("jsonrpc", "2.0") != "2.0":
                raise McpProtocolError("MCP response is not JSON-RPC 2.0")
            return message
        self._terminate()
        raise McpTimeoutError(
            f"MCP server timed out after {timeout:g}s on '{method}'; "
            f"the server process was terminated and the connection is "
            f"unavailable until it is recreated"
        )

    def _read_response_line(self, block_timeout: float) -> Optional[bytes]:
        """Return the next response line from the background reader thread,
        bounded by a block timeout (None on timeout, EOF or process death)."""
        try:
            return self._response_queue.get(timeout=block_timeout)
        except queue.Empty:
            return None

    def _ensure_alive(self) -> None:
        """Raise when the server process is reported dead."""
        if self.process is None or self.process.poll() is not None:
            raise McpClientError(
                "MCP server process died unexpectedly"
            )

    # ── public operations ────────────────────────────────────────────────

    def list_tools(self, timeout: float = LIST_TOOLS_TIMEOUT) -> Dict[str, Any]:
        """Return the raw result of ``tools/list`` (cached per process life).

        The cache is keyed to the live process: when the process dies the
        cache becomes invalid and the next call raises until the client is
        recreated.
        """
        self._ensure_started()
        if self._tools_cache is not None:
            self._ensure_alive()
            return self._tools_cache
        with self._call_lock:
            if self._tools_cache is not None:
                return self._tools_cache
            response = self._request("tools/list", {}, timeout=timeout)
            result = response.get("result")
            if "error" in response:
                raise McpClientError(
                    f"tools/list failed: {_format_error(response['error'])}"
                )
            if not isinstance(result, dict) or not isinstance(
                    result.get("tools"), list):
                raise McpProtocolError(
                    "MCP tools/list returned an invalid result"
                )
            self._tools_cache = result
            return result

    def call_tool(self, tool_name: str,
                  arguments: Optional[Dict[str, Any]] = None,
                  timeout: float = CALL_TOOL_TIMEOUT,
                  ) -> Dict[str, Any]:
        """Call a tool and return ``{"content": str, "is_error": bool}``.

        Content blocks of type "text" are concatenated (separated by blank
        lines). Any non-text content block (image, audio, resource, ...)
        makes the call fail with the plain error
        ``non-text content is not supported``. Server-reported tool errors
        (``isError: true``) carry the decoded message/data; ids are checked
        by the transport layer.
        """
        name = str(tool_name or "").strip()
        if not name:
            raise McpClientError("Missing required argument: tool_name")
        self._ensure_started()
        self._ensure_alive()
        params: Dict[str, Any] = {"name": name}
        if arguments is not None:
            if not isinstance(arguments, dict):
                raise McpClientError(
                    "MCP tool arguments must be a JSON object"
                )
            params["arguments"] = arguments
        with self._call_lock:
            response = self._request("tools/call", params, timeout=timeout)
        if "error" in response:
            raise McpClientError(
                f"tools/call failed: {_format_error(response['error'])}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError("MCP tools/call returned an invalid result")
        is_error = bool(result.get("isError", False))
        content = result.get("content")
        if not isinstance(content, list):
            raise McpProtocolError(
                "MCP tools/call result has no content list"
            )
        text_parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                text_parts.append(str(block))
                continue
            if block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
            else:
                raise McpClientError(
                    "MCP tool returned non-text content "
                    f"(type: {block.get('type')!r}); non-text results "
                    "are not supported"
                )
        return {"content": "\n\n".join(text_parts), "is_error": is_error}

    def _ensure_started(self) -> None:
        if not self.started:
            raise McpClientError("MCP server is not started")
        if self.dead:
            raise McpClientError(
                "MCP connection is unavailable after timeout/termination; "
                "recreate the connection (no automatic retry)"
            )

    def _terminate(self) -> None:
        """Kill the server process (timeout path). No retry is performed."""
        self.dead = True
        proc = self.process
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
            except (OSError, ValueError):
                pass
        self._unregister_self()

    def close(self) -> None:
        """Close the client: terminate the process and drop the registry
        entry. Called both on recreate/potential cleanup; idempotent. The
        process reference is kept so ``process.poll()`` stays inspectable
        after the close (dead=True blocks all further calls anyway)."""
        self.dead = True
        proc = self.process
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
            except (OSError, ValueError):
                pass
        self._unregister_self()


def _format_error(error_obj: Any) -> str:
    """Format a JSON-RPC error object as a readable message."""
    err = error_obj if isinstance(error_obj, dict) else {}
    code = err.get("code", "?")
    message = str(err.get("message") or "unknown error")
    data = err.get("data")
    if data is not None:
        try:
            data_repr = json.dumps(data, ensure_ascii=False)[:500]
        except (TypeError, ValueError):
            data_repr = str(data)[:500]
        return f"[{code}] {message} - data: {data_repr}"
    return f"[{code}] {message}"


# ─── per-connection factory and cache helpers ───────────────────────────────


def get_client(conn_id: str, command: str, args: Optional[List[str]],
               env: Optional[Dict[str, str]] = None) -> "McpClient":
    """Return the live client for *conn_id*, starting a new one if needed.

    When the cached client is dead (timeout / was killed / its process is
    gone), recreate it WITHOUT retrying the operation that killed it - the
    caller decides what the user sees. One client per connection; creating
    a new one kills and replaces the previous process.
    """
    with _PROCESS_REGISTRY_LOCK:
        existing = _PROCESS_REGISTRY.get(conn_id)
        if existing is not None and not existing.dead:
            try:
                existing._ensure_alive()
                return existing
            except McpClientError:
                pass
        client = McpClient(command, args, env, server_key=conn_id)
        client.start()
        return client


# Registry for HTTP clients keyed by connection id (same one-client-per-
# connection semantics as the stdio transport).
_HTTP_REGISTRY: Dict[str, "McpHttpClient"] = {}
_HTTP_REGISTRY_LOCK = threading.RLock()


def get_http_client(conn_id: str, url: str,
                    env: Optional[Dict[str, str]] = None,
                    auth_header: Optional[str] = None) -> "McpHttpClient":
    """Return the live HTTP client for *conn_id*, starting one if needed.

    Mirror of ``get_client`` for the http transport: one session per
    connection, a dead session is replaced by a fresh one (without
    retrying the operation that killed it).
    """
    with _HTTP_REGISTRY_LOCK:
        existing = _HTTP_REGISTRY.get(conn_id)
        if existing is not None and not existing.dead:
            return existing
    client = McpHttpClient(
        url, env=env, auth_header=auth_header, server_key=conn_id
    )
    client.start()
    with _HTTP_REGISTRY_LOCK:
        existing = _HTTP_REGISTRY.get(conn_id)
        if existing is not None:
            existing.close()
        _HTTP_REGISTRY[conn_id] = client
    return client


def _drop_http_client(conn_id: str) -> None:
    """Close and remove the cached HTTP client of *conn_id*."""
    with _HTTP_REGISTRY_LOCK:
        existing = _HTTP_REGISTRY.get(conn_id)
        if existing is not None:
            existing.close()
            _HTTP_REGISTRY.pop(conn_id, None)


def drop_client(conn_id: str) -> None:
    """Drop and close the cached client of *conn_id* (used on reconnect /
    configuration changes). Clears both the stdio and the http registries."""
    _drop_http_client(conn_id)
    with _PROCESS_REGISTRY_LOCK:
        existing = _PROCESS_REGISTRY.get(conn_id)
        if existing is not None:
            existing.close()
            _PROCESS_REGISTRY.pop(conn_id, None)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
