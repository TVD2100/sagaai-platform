# -*- coding: utf-8 -*-
"""
core.connectors - CRUD for external service connections.

Each connection lives in ``DATA_DIR/connectors/<id>/manifest.json``:

    {
        "id": "<uuid8>",
        "service": "github_rest" | "ssh" | "mcp",
        "name": "GitHub-TVD2100",          # user-visible name
        "account": "TVD2100",              # display info about the account
        "token_encrypted": "<fernet>",     # token-style services (github_rest)
        "config": {...},                   # non-secret settings (SSH: host, port, username)
        "secrets_encrypted": {...},        # per-field encrypted secrets (SSH: password, private_key)
        "created_at": "...",
        "updated_at": "..."
    }

Secrets are never stored in plain text inside the manifest. Write functions
accept plaintext secrets and encrypt them via ``core.crypto.encrypt`` before
persisting. Read functions never return secrets; they expose masked views
(``token_masked``, ``has_secrets``) for the UI and provide
``decrypt_token`` / ``decrypt_secret`` / ``get_connection_secrets`` for the
service connectors.

Draft connections (``pending=True``) store only the non-secret config and
carry ``credentials_status: "pending"``; they appear on the Connectors page
waiting for credentials. Filling credentials later (``update_connection``,
``set_connection_token``, ``set_connection_secret``) moves the status to
"active".

Each connection folder may also hold a free-form ``memory.md`` file - the
agent-maintained notes about the server/account (already discovered limits,
paths, gotchas). It is managed with ``get_connection_memory`` and
``set_connection_memory`` and must never contain secrets.

No streamlit imports; errors raise ValueError with a user-facing message.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.fs import ensure_dir
from core.crypto import encrypt, decrypt
from cryptography.fernet import InvalidToken
from core.paths import DATA_DIR


# Root directory where all connectors live.
CONNECTORS_DIR: str = os.path.join(DATA_DIR, "connectors")

# Known services. Future connectors (gitlab, slack, ...) extend this registry.
CONNECTOR_SERVICES: Dict[str, Dict[str, Any]] = {
    "github_rest": {
        "name": "GitHub REST API (direct)",
        "description": "Direct GitHub REST API v3 connection (requests, batch publishing via Git Data API)",
        "icon": "\U0001f419",
    },
    "ssh": {
        "name": "SSH Server",
        "description": "SSH/SFTP connection to a remote server (paramiko): run commands, list/read/write files",
        "icon": "\U0001f511",
        "config_fields": ["host", "port", "username"],
        "secret_fields": ["password", "private_key", "key_passphrase"],
    },
    "mcp": {
        "name": "MCP Server",
        "description": "Model Context Protocol server (local stdio process or HTTP endpoint); its tools appear in the orchestrator tool catalog",
        "icon": "\U0001f50c",
        "config_fields": ["transport", "command", "args", "url", "auth_header", "preset", "description"],
        "secret_fields": ["env_json"],
    },
}

# Interpreter whitelist for the mcp service: bare executable names only,
# paths and arbitrary binaries are never accepted (no shell, no surprises).
MCP_ALLOWED_COMMANDS = ("python", "python3", "node", "npx", "uvx", "uv")


# URL rules for the http transport: remote servers must use https; plain
# http is accepted only for local development addresses (SSRF protection).
_LOCAL_HTTP_HOSTS = ("localhost", "127.0.0.1", "::1")
_MAX_MCP_URL_LEN = 500
_MAX_MCP_AUTH_HEADER_LEN = 300
_AUTH_HEADER_PLACEHOLDER_RE = re.compile(r"\{([^}]*)\}")
_MCP_PRESET_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_MCP_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_mcp_url(url: str) -> None:
    """Validate the http-transport url (untrusted input, SSRF-safe)."""
    if len(url) > _MAX_MCP_URL_LEN:
        raise ValueError("MCP url is too long")
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ValueError("MCP url is invalid")
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme == "https" and host:
        return
    if scheme == "http" and host in _LOCAL_HTTP_HOSTS:
        return
    raise ValueError(
        "MCP url must be https:// (or http:// for localhost only)"
    )


def _validate_mcp_auth_header(template: str) -> None:
    """Validate an Authorization header template (no real secrets inside).

    Placeholders like ``{GITHUB_TOKEN}`` are resolved at request time from
    the decrypted env_json pairs; the template itself must be a single line
    without control characters (header injection) and use uppercase
    placeholder names only.
    """
    if len(template) > _MAX_MCP_AUTH_HEADER_LEN:
        raise ValueError("MCP auth_header is too long")
    if any(ord(ch) < 32 for ch in template):
        raise ValueError("MCP auth_header must be a single header line")
    groups = _AUTH_HEADER_PLACEHOLDER_RE.findall(template)
    stripped = _AUTH_HEADER_PLACEHOLDER_RE.sub("", template)
    if "{" in stripped or "}" in stripped:
        raise ValueError("MCP auth_header has malformed placeholder braces")
    for name in groups:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError(
                "MCP auth_header placeholders must be uppercase env names: "
                f"{{{name}}}"
            )


def _normalize_mcp_secret_fields(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Validate the optional 'secret_env_fields' metadata of an mcp config.

    Maps a user-friendly field id ('token', 'login', ...) to the env var the
    server expects ('env') plus UI metadata (label/help/password). No secret
    values live here - only the mapping. Entry specs may be plain env-name
    strings or {env, label, help, password} objects.
    """
    if not isinstance(raw, dict):
        raise ValueError("MCP secret_env_fields must be a JSON object")
    if len(raw) > 8:
        raise ValueError("MCP secret_env_fields: at most 8 fields")
    out: Dict[str, Dict[str, Any]] = {}
    for field_id, spec in raw.items():
        fid = str(field_id or "").strip().lower()
        if not _MCP_PRESET_RE.fullmatch(fid):
            raise ValueError("MCP secret field id must be [a-z0-9_-]")
        if isinstance(spec, str):
            env_name = spec
            meta: Dict[str, Any] = {}
        elif isinstance(spec, dict):
            env_name = str(spec.get("env") or "")
            meta = {
                "label": str(spec.get("label") or "").strip()[:100],
                "help": str(spec.get("help") or "").strip()[:300],
                "password": bool(spec.get("password", True)),
            }
        else:
            raise ValueError(
                "MCP secret field entry must be a string or an object"
            )
        env_name = (env_name or "").strip()
        if not _MCP_ENV_NAME_RE.fullmatch(env_name):
            raise ValueError(
                f"Invalid MCP env var name in secret field '{fid}': "
                f"{env_name!r}"
            )
        out[fid] = {"env": env_name, **meta}
    return out


def _normalize_mcp_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate the mcp config: stdio (whitelisted command + args) or http (url).

    Optional metadata keys (``preset``, ``description``) are validated and
    stored for the UI and for preset instantiation; they carry no secrets.
    """
    cfg = config if isinstance(config, dict) else {}
    transport = str(cfg.get("transport") or "").strip().lower() or "stdio"
    if transport == "http":
        url = str(cfg.get("url") or "").strip()
        if not url:
            raise ValueError("MCP url cannot be empty for the http transport")
        _validate_mcp_url(url)
        if str(cfg.get("command") or "").strip():
            raise ValueError("MCP http transport does not use command/args")
        norm: Dict[str, Any] = {"transport": "http", "url": url}
        template = str(cfg.get("auth_header") or "").strip()
        if template:
            _validate_mcp_auth_header(template)
            norm["auth_header"] = template
    elif transport == "stdio":
        command = str(cfg.get("command") or "").strip()
        if not command:
            raise ValueError("MCP command cannot be empty")
        if command.lower() not in MCP_ALLOWED_COMMANDS:
            if os.path.isabs(command) and os.path.basename(command).lower() in MCP_ALLOWED_COMMANDS:
                if not (os.path.isfile(command) and os.access(command, os.X_OK)):
                    raise ValueError(
                        "MCP command path is not an executable interpreter"
                    )
            else:
                raise ValueError(
                    "MCP command is not allowed; whitelist: "
                    + ", ".join(MCP_ALLOWED_COMMANDS)
                )
        args_raw = cfg.get("args")
        if args_raw in (None, ""):
            args: List[str] = []
        elif isinstance(args_raw, list):
            args = [str(a) for a in args_raw]
            if any(not a or not str(a).strip() for a in args_raw):
                raise ValueError("MCP args entries must be non-empty strings")
        else:
            raise ValueError("MCP args must be a list of strings")
        norm = {"transport": "stdio", "command": command, "args": args}
    else:
        raise ValueError("MCP transport must be 'stdio' or 'http'")
    preset = str(cfg.get("preset") or "").strip()
    if preset:
        if not _MCP_PRESET_RE.fullmatch(preset):
            raise ValueError("MCP preset id must be [a-z0-9_-], up to 64 chars")
        norm["preset"] = preset
    secret_fields_raw = cfg.get("secret_env_fields")
    if secret_fields_raw not in (None, {}):
        norm["secret_env_fields"] = _normalize_mcp_secret_fields(
            secret_fields_raw
        )
    description = str(cfg.get("description") or "").strip()
    if description:
        norm["description"] = description[:500]
    return norm


_MCP_SECRET_KEY_NAMES = ("env_json", "env_pairs")


def _normalize_mcp_env_pairs(pairs: Any,
                             env_names: Optional[set] = None) -> Dict[str, str]:
    """Validate plain env-var pairs ({NAME: value}); keep non-empty only.

    Names must match the env-var pattern and, when ``env_names`` is given,
    must be listed there (the connection's declared secret fields).
    """
    data = pairs if isinstance(pairs, dict) else {}
    out: Dict[str, str] = {}
    for raw_key, raw_val in data.items():
        key = str(raw_key or "").strip()
        if not _MCP_ENV_NAME_RE.fullmatch(key):
            raise ValueError(f"Invalid MCP env var name: {raw_key!r}")
        if env_names is not None and key not in env_names:
            raise ValueError(f"Unknown MCP env var for this connection: {key}")
        val = str(raw_val or "")
        if val.strip():
            out[key] = val
    return out


def _mcp_env_json_to_pairs(env_json: str,
                           env_names: Optional[set] = None) -> Dict[str, str]:
    """Parse a user-submitted env_json string into validated pairs."""
    text = str(env_json or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("MCP env_json must be valid JSON")
    if not isinstance(parsed, dict):
        raise ValueError("MCP env_json must be a JSON object")
    out: Dict[str, str] = {}
    for raw_key, raw_val in parsed.items():
        key = str(raw_key or "").strip()
        if not key:
            raise ValueError("MCP env_json keys cannot be empty")
        if env_names is not None and key not in env_names:
            raise ValueError(f"Unknown MCP env var for this connection: {key}")
        if isinstance(raw_val, (dict, list)):
            raise ValueError("MCP env_json values must be scalars")
        out[key] = str(raw_val)
    return out


def _read_saved_mcp_env(env_json: str) -> Dict[str, str]:
    """Parse a stored env_json string leniently (bad data yields {})."""
    text = str(env_json or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: Dict[str, str] = {}
    for key, val in parsed.items():
        key_str = str(key or "").strip()
        if key_str and not isinstance(val, (dict, list)):
            out[key_str] = str(val)
    return out


def _current_mcp_env(data: Dict[str, Any]) -> Dict[str, str]:
    """Return the decrypted env pairs currently stored on an mcp manifest."""
    secrets_enc = data.get("secrets_encrypted")
    if not isinstance(secrets_enc, dict) or not secrets_enc.get("env_json"):
        return {}
    try:
        return _read_saved_mcp_env(decrypt(secrets_enc["env_json"]))
    except InvalidToken:
        return {}


def _normalize_mcp_secrets(secrets: Optional[Dict[str, Any]],
                           env_names: Optional[set] = None) -> Dict[str, str]:
    """Validate the mcp secrets: ``env_json`` (full JSON object) and/or
    ``env_pairs`` (plain {NAME: value} dict).

    Both keys may be given: env_pairs entries then override the env_json
    entries of the same name. The result is always a single
    ``{"env_json": "<one-line json>"}`` entry with the merged values; empty
    input yields no secrets.
    """
    data = secrets if isinstance(secrets, dict) else {}
    for key in data:
        if str(key) not in _MCP_SECRET_KEY_NAMES:
            raise ValueError(f"Unknown secret field for service 'mcp': {key}")
    raw = data.get("env_json")
    pairs_db: Dict[str, str] = {}
    if raw not in (None, ""):
        pairs_db = _mcp_env_json_to_pairs(raw, env_names)
    pairs_extra = _normalize_mcp_env_pairs(data.get("env_pairs"), env_names)
    if not pairs_db and not pairs_extra:
        return {}
    merged = dict(pairs_db)
    merged.update(pairs_extra)
    return {
        "env_json": json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
    }


_VALID_ID_RE = re.compile(r"^[a-z0-9_-]+$")

# Secret field names inside a connection manifest (password, private_key, ...).
_SECRET_FIELD_RE = re.compile(r"^[a-z0-9_]+$")

# Agent-maintained per-connection notes file; never stores credentials.
CONNECTION_MEMORY_FILE: str = "memory.md"
CONNECTION_MEMORY_MAX_BYTES: int = 64 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connectors_root() -> str:
    """Return the connectors root dir. Reads DATA_DIR at call time so tests
    can override ``core.paths.DATA_DIR`` (and thus re-compute)."""
    import core.paths
    return os.path.join(core.paths.DATA_DIR, "connectors")


def _manifest_path(conn_id: str) -> str:
    if not _VALID_ID_RE.fullmatch(conn_id or "") or conn_id in (".", ".."):
        raise ValueError("Invalid connection id")
    return os.path.join(_connectors_root(), conn_id, "manifest.json")


def _manifest_read(conn_id: str) -> Dict[str, Any]:
    """Read a manifest as stored on disk (encrypted token field preserved)."""
    path = _manifest_path(conn_id)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Connection not found: {conn_id}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise ValueError(f"Cannot read connection manifest: {e}")
    if not isinstance(data, dict):
        raise ValueError("Corrupted connection manifest")
    return data


def _manifest_write(conn_id: str, data: Dict[str, Any]) -> None:
    """Persist the manifest after ensuring the parent folder exists."""
    ensure_dir(os.path.join(_connectors_root(), conn_id))
    path = _manifest_path(conn_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _validate_service(service: str) -> str:
    svc = (service or "").strip().lower()
    if svc not in CONNECTOR_SERVICES:
        raise ValueError("Unsupported connector service")
    return svc


def _unique_conn_id() -> str:
    """Return a short unique id for a new connection folder."""
    conn_id = uuid.uuid4().hex[:8]
    while os.path.isdir(os.path.join(_connectors_root(), conn_id)):
        conn_id = uuid.uuid4().hex[:8]
    return conn_id


def _service_field_names(service: str, key: str) -> List[str]:
    """Return declared field names (config_fields/secret_fields) of a service."""
    svc = CONNECTOR_SERVICES.get(service) or {}
    fields = svc.get(key)
    return [str(f) for f in fields] if isinstance(fields, list) else []


def _secret_field_names(service: str) -> List[str]:
    """Return the secret field names declared by a connector service."""
    return _service_field_names(service, "secret_fields")


def _normalize_config(service: str, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate and normalize the non-secret config of a connection.

    SSH: host and username are required; port defaults to 22 and must be an
    integer in 1..65535. MCP: whitelisted ``command`` and an ``args`` string
    list. Other services have no declared config fields and yield an empty
    dict.
    """
    if service == "mcp":
        return _normalize_mcp_config(config)
    if service != "ssh":
        return {}
    cfg = config if isinstance(config, dict) else {}
    host = str(cfg.get("host") or "").strip()
    username = str(cfg.get("username") or "").strip()
    if not host:
        raise ValueError("SSH host cannot be empty")
    if not username:
        raise ValueError("SSH username cannot be empty")
    port_raw = cfg.get("port")
    if port_raw in (None, ""):
        port = 22
    else:
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            raise ValueError("SSH port must be an integer")
        if port < 1 or port > 65535:
            raise ValueError("SSH port must be in range 1..65535")
    return {"host": host, "port": port, "username": username}


def _normalize_secrets(service: str, secrets: Optional[Dict[str, Any]],
                       env_names: Optional[set] = None) -> Dict[str, str]:
    """Validate secret fields of a connection; unknown fields are rejected.

    Only non-empty values are kept, so empty inputs mean "keep existing".
    MCP validates the ``env_json`` / ``env_pairs`` secrets through its own
    normalizer (``env_names`` optionally restricts the accepted env var
    names to the connection's declared secret fields).
    """
    if service == "mcp":
        return _normalize_mcp_secrets(secrets, env_names=env_names)
    if service != "ssh":
        return {}
    allowed = set(_secret_field_names(service))
    data = secrets if isinstance(secrets, dict) else {}
    out: Dict[str, str] = {}
    for raw_key, raw_val in data.items():
        key = str(raw_key or "").strip()
        if not _SECRET_FIELD_RE.fullmatch(key):
            raise ValueError(f"Invalid secret field name: {raw_key}")
        if key not in allowed:
            raise ValueError(f"Unknown secret field for service '{service}': {key}")
        val = str(raw_val or "")
        if val.strip():
            out[key] = val
    return out


def _validate_ssh_auth(secrets: Dict[str, str]) -> None:
    """Require password or private key; passphrase only accompanies a key."""
    if not secrets.get("password") and not secrets.get("private_key"):
        raise ValueError(
            "SSH connection requires either a password or a private key"
        )
    if secrets.get("key_passphrase") and not secrets.get("private_key"):
        raise ValueError(
            "key_passphrase can only be used together with private_key"
        )


def public_manifest(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a manifest dict with secrets removed, suitable for the UI/API.

    Token-style services (github_rest) expose ``has_token`` /
    ``token_masked``. Per-field secrets (``secrets_encrypted``, e.g. SSH
    password / private key) are replaced by ``has_secrets`` and
    ``secrets_masked`` - only field names are exposed, never values.
    Non-secret ``config`` fields (SSH host, port, username) stay visible.
    """
    out = dict(data)
    out.pop("token_encrypted", None)
    token = data.get("token_encrypted", "")
    out["has_token"] = bool(token)
    out["token_masked"] = "***" if token else ""
    secrets = data.get("secrets_encrypted")
    out.pop("secrets_encrypted", None)
    if isinstance(secrets, dict) and secrets:
        out["has_secrets"] = True
        out["secrets_masked"] = {str(k): "***" for k in sorted(secrets)}
    else:
        out["has_secrets"] = False
        out["secrets_masked"] = {}
    config = data.get("config")
    if isinstance(config, dict) and config:
        out["config"] = dict(config)
    if not out.get("credentials_status"):
        out["credentials_status"] = (
            "active" if (out["has_token"] or out["has_secrets"]) else "pending"
        )
    return out


def list_connections() -> List[Dict[str, Any]]:
    """Return all connection manifests in the connectors directory.

    Results are sorted by name (case-insensitive). Each entry is a public
    manifest (no token).
    """
    root = _connectors_root()
    result = []
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return result
    for name in names:
        path = os.path.join(root, name, "manifest.json")
        if not os.path.isfile(path):
            continue
        try:
            data = _manifest_read(name)
            result.append(public_manifest(data))
        except Exception:
            continue
    return sorted(result, key=lambda c: (c.get("name") or "").lower())


def get_connection(conn_id: str) -> Dict[str, Any]:
    """Return a public manifest for *conn_id*; {} when missing."""
    try:
        data = _manifest_read(conn_id)
    except Exception:
        return {}
    return public_manifest(data)


def get_connection_full(conn_id: str) -> Optional[Dict[str, Any]]:
    """Return the raw manifest (including encrypted token) for internal use."""
    try:
        return _manifest_read(conn_id)
    except Exception:
        return None


def create_connection(service: str, name: str, token: str = "",
                      account: str = "",
                      config: Optional[Dict[str, Any]] = None,
                      secrets: Optional[Dict[str, Any]] = None,
                      pending: bool = False) -> Dict[str, Any]:
    """Create a new connection.

    Args:
        service: connector service id ("github_rest", "ssh").
        name: user-visible name (e.g. "GitHub-TVD2100").
        token: plaintext API token (token-style services such as
            github_rest); encrypted before storage.
        account: display account/login info, optional.
        config: non-secret settings of the service (SSH: host, port,
            username; MCP: command, args).
        secrets: per-field plaintext secrets (SSH: password, private_key,
            key_passphrase; MCP: env_json, optional); each value is
            encrypted before storage.
        pending: when True, create a draft connection without credentials
            (``credentials_status="pending"``); the non-secret ``config`` is
            still validated and stored, secrets/token are ignored.

    Returns the created public manifest.
    """
    svc = _validate_service(service)
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Connection name cannot be empty")

    now = _now()
    conn_id = _unique_conn_id()
    manifest: Dict[str, Any] = {
        "id": conn_id,
        "service": svc,
        "name": clean_name,
        "account": (account or "").strip(),
        "created_at": now,
        "updated_at": now,
    }
    if svc == "mcp":
        # Config is validated and stored in both modes (a draft still
        # needs the transport config to become useful). Secrets are
        # merged into the existing stored environment (env_pairs form).
        manifest["config"] = _normalize_config(svc, config)
        if pending:
            manifest["credentials_status"] = "pending"
        else:
            env_names = None
            declared = manifest["config"].get("secret_env_fields", {})
            if isinstance(declared, dict) and declared:
                env_names = {
                    str(v.get("env") or "") for v in declared.values()
                    if isinstance(v, dict) and v.get("env")
                } or None
            normalized_secrets = _normalize_secrets(svc, secrets,
                                                    env_names=env_names)
            manifest["secrets_encrypted"] = {
                key: encrypt(val) for key, val in normalized_secrets.items()
            }
            manifest["credentials_status"] = "active"
    elif svc == "ssh":
        # Config is validated and stored in both modes (a draft still
        # needs host/username to become useful once credentials arrive).
        manifest["config"] = _normalize_config(svc, config)
        if pending:
            manifest["credentials_status"] = "pending"
        else:
            normalized_secrets = _normalize_secrets(svc, secrets)
            _validate_ssh_auth(normalized_secrets)
            manifest["secrets_encrypted"] = {
                key: encrypt(val) for key, val in normalized_secrets.items()
            }
            manifest["credentials_status"] = "active"
    else:
        if pending:
            manifest["credentials_status"] = "pending"
        else:
            token = token or ""
            if not token.strip():
                raise ValueError("Token cannot be empty")
            manifest["token_encrypted"] = encrypt(token.strip())
            manifest["credentials_status"] = "active"
    _manifest_write(conn_id, manifest)
    return public_manifest(manifest)


def update_connection(conn_id: str, name: str = "", account: str = "",
                      token: str = "",
                      config: Optional[Dict[str, Any]] = None,
                      secrets: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Update a connection's display fields and optionally its secrets.

    Args:
        conn_id: id of the connection to update.
        name: new display name (empty = keep).
        account: new account display string (empty = keep unless token set).
        token: when non-empty, replaces the stored encrypted token
            (token-style services).
        config: non-secret settings to merge (SSH: host, port, username;
            MCP: command, args).
        secrets: per-field plaintext secrets to replace; empty values keep
            the stored secret.

    Returns the updated public manifest.
    """
    data = _manifest_read(conn_id)
    service = str(data.get("service") or "")
    if (name or "").strip():
        data["name"] = name.strip()
    if (account or "").strip():
        data["account"] = account.strip()
    if (token or "").strip():
        data["token_encrypted"] = encrypt(token.strip())
        data["credentials_status"] = "active"
    if service == "mcp":
        if isinstance(config, dict) and config:
            merged_cfg = dict(data.get("config") or {})
            merged_cfg.update(config)
            data["config"] = _normalize_config(service, merged_cfg)
            # Command/args changed: kill the cached server process so the
            # next call starts a fresh one.
            conn_id_now = data.get("id")
            if conn_id_now:
                try:
                    from core.mcp_client import drop_client as _drop_client
                    _drop_client(str(conn_id_now))
                except Exception:
                    pass
        normalized_secrets = _normalize_secrets(service, secrets,
                                                env_names=get_mcp_env_field_names(conn_id) or None)
        if normalized_secrets:
            if secrets and isinstance(secrets, dict) and "env_pairs" in secrets:
                # env_pairs merges key-by-key into the currently stored
                # environment; env_json (full object) replaces it wholesale.
                merged_env = _current_mcp_env(data)
                incoming = _mcp_env_json_to_pairs(normalized_secrets["env_json"])
                merged_env.update(incoming)
                data["secrets_encrypted"] = {
                    "env_json": encrypt(json.dumps(
                        merged_env, ensure_ascii=False, separators=(",", ":")))
                }
            else:
                merged_enc = dict(data.get("secrets_encrypted") or {})
                for key, val in normalized_secrets.items():
                    merged_enc[key] = encrypt(val)
                data["secrets_encrypted"] = merged_enc
            data["credentials_status"] = "active"
            conn_id_now = data.get("id")
            if conn_id_now:
                # The cached client snapshots the decrypted env at start;
                # secrets changed, so drop it and let the next call rebuild.
                try:
                    from core.mcp_client import drop_client as _drop_client
                    _drop_client(str(conn_id_now))
                except Exception:
                    pass
    if service == "ssh":
        if isinstance(config, dict) and config:
            merged_cfg = dict(data.get("config") or {})
            merged_cfg.update(config)
            data["config"] = _normalize_config(service, merged_cfg)
        normalized_secrets = _normalize_secrets(service, secrets)
        if normalized_secrets:
            merged_enc = dict(data.get("secrets_encrypted") or {})
            for key, val in normalized_secrets.items():
                merged_enc[key] = encrypt(val)
            if not merged_enc.get("password") and not merged_enc.get("private_key"):
                raise ValueError(
                    "SSH connection requires either a password or a private key"
                )
            data["secrets_encrypted"] = merged_enc
            data["credentials_status"] = "active"
    data["updated_at"] = _now()
    _manifest_write(conn_id, data)
    return public_manifest(data)


def set_connection_token(conn_id: str, token: str) -> bool:
    """Replace the encrypted token for a connection. Returns True on success."""
    if not (token or "").strip():
        raise ValueError("Token cannot be empty")
    data = _manifest_read(conn_id)
    data["token_encrypted"] = encrypt(token.strip())
    data["credentials_status"] = "active"
    data["updated_at"] = _now()
    _manifest_write(conn_id, data)
    return True


def delete_connection(conn_id: str, *, record_removal: bool = True) -> bool:
    """Delete a connection folder. Returns True on success.

    Also drops the cached MCP server process of the connection (when any),
    so a deleted connector never leaves a live server behind. A connection
    that a catalog install created is recorded as deliberately removed, so
    the showcase stops calling it installed and the entity is never offered
    as a hidden re-install.

    *record_removal* is turned off by an installer undoing its own run: a
    connection removed because the install failed was never installed, and
    marking it removed would describe a deletion the user never made.
    """
    try:
        from core.mcp_client import drop_client as _drop_client
        _drop_client(str(conn_id))
    except Exception:
        pass
    d = os.path.join(_connectors_root(), conn_id)
    try:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        if record_removal:
            _mark_marketplace_removed(conn_id)
        return True
    except Exception:
        return False


def _mark_marketplace_removed(conn_id: str) -> None:
    """Record a deliberate deletion of a catalog-installed MCP connection.

    An MCP entity owns no folder - its installation is the connection - so
    deleting the connection is the uninstall. Without this marker the
    registry keeps the row, the showcase shows the server as installed while
    it is gone, and reinstalling it is silently a no-op.

    The row is found by the connection id the installer stored as its runtime
    slug. A connection the catalog never installed has no row and stays a
    purely local deletion; a failed install has no row yet either, so a
    rollback does not leave a removal marker behind.
    """
    try:
        from core.marketplace import registry as marketplace_registry

        for row in marketplace_registry.list_installed("mcp"):
            if str(row.get("runtime_slug") or "") != str(conn_id):
                continue
            entity_id = str(row.get("id") or "")
            if not entity_id:
                return
            marketplace_registry.remove_installed("mcp", entity_id)
            marketplace_registry.mark_removed(
                "mcp", entity_id, str(conn_id), via="user"
            )
            return
    except Exception:
        pass


def decrypt_token(conn_id: str) -> str:
    """Decrypt and return the plaintext token for a connection.

    Raises:
        ValueError: if the connection is missing or decryption fails.
    """
    data = _manifest_read(conn_id)
    if data.get("credentials_status") == "pending":
        raise ValueError("Connection credentials have not been set yet")
    token_enc = data.get("token_encrypted", "")
    if not token_enc:
        raise ValueError("Connection has no token")
    try:
        return decrypt(token_enc)
    except InvalidToken:
        raise ValueError("Cannot decrypt connection token (invalid encryption key)")


def get_connection_secrets(conn_id: str) -> Dict[str, str]:
    """Decrypt and return ALL secret fields of a connection.

    Returns {field_name: plaintext} (e.g. SSH: {"password": "..."}).
    Fields whose ciphertext cannot be decrypted are skipped. Used by
    service connectors that need the full credential set at once.

    Raises:
        ValueError: if the connection is missing or has no secret fields.
    """
    data = _manifest_read(conn_id)
    if data.get("credentials_status") == "pending":
        raise ValueError("Connection credentials have not been set yet")
    secrets_enc = data.get("secrets_encrypted")
    if not isinstance(secrets_enc, dict) or not secrets_enc:
        raise ValueError("Connection has no secret fields")
    result: Dict[str, str] = {}
    for field, token_enc in secrets_enc.items():
        if not token_enc:
            continue
        try:
            result[str(field)] = decrypt(token_enc)
        except InvalidToken:
            continue
    return result


def decrypt_secret(conn_id: str, field: str) -> str:
    """Decrypt and return one named secret field of a connection.

    Raises:
        ValueError: if the connection/field is missing or decryption fails.
    """
    field_name = (field or "").strip()
    if not field_name:
        raise ValueError("Secret field name cannot be empty")
    data = _manifest_read(conn_id)
    if data.get("credentials_status") == "pending":
        raise ValueError("Connection credentials have not been set yet")
    secrets_enc = data.get("secrets_encrypted")
    if not isinstance(secrets_enc, dict) or field_name not in secrets_enc:
        raise ValueError(f"Connection has no secret field: {field_name}")
    token_enc = secrets_enc.get(field_name) or ""
    if not token_enc:
        raise ValueError(f"Secret field is empty: {field_name}")
    try:
        return decrypt(token_enc)
    except InvalidToken:
        raise ValueError(
            f"Cannot decrypt secret field '{field_name}' (invalid encryption key)"
        )


def set_connection_secret(conn_id: str, field: str, value: str) -> bool:
    """Store/replace one encrypted secret field on a connection.

    Only fields declared by the connection's service registry entry are
    accepted. Returns True on success.

    Raises:
        ValueError: on unknown field names or empty values.
    """
    field_name = (field or "").strip()
    if not field_name:
        raise ValueError("Secret field name cannot be empty")
    val = str(value or "")
    if not val.strip():
        raise ValueError("Secret value cannot be empty")
    data = _manifest_read(conn_id)
    service = str(data.get("service") or "")
    allowed = set(_secret_field_names(service))
    if field_name not in allowed:
        raise ValueError(
            f"Unknown secret field for service '{service}': {field_name}"
        )
    if service == "mcp" and field_name == "env_json":
        normalized = _normalize_mcp_secrets({"env_json": val})
        val = normalized["env_json"]
    secrets_enc = dict(data.get("secrets_encrypted") or {})
    secrets_enc[field_name] = encrypt(val)
    data["secrets_encrypted"] = secrets_enc
    data["credentials_status"] = "active"
    data["updated_at"] = _now()
    _manifest_write(conn_id, data)
    return True


def get_mcp_env_field_names(conn_id: str) -> set:
    """Return the env var names of the MCP connection's secret fields.

    Built from the stored ``secret_env_fields`` mapping (keys: field ids;
    ``env`` values: the env var names). Returns a set of env names, never
    their values.
    """
    data = get_connection_full(conn_id)
    if not isinstance(data, dict):
        return set()
    cfg = data.get("config")
    fields = cfg.get("secret_env_fields") if isinstance(cfg, dict) else None
    if not isinstance(fields, dict):
        return set()
    return {str(v.get("env") or "") for v in fields.values()
            if isinstance(v, dict) and v.get("env")}


def get_mcp_env_var_names(conn_id: str) -> List[str]:
    """Return the env variable names stored on an MCP connection (no values)."""
    data = get_connection_full(conn_id)
    if not isinstance(data, dict):
        raise ValueError(f"Connection not found: {conn_id}")
    if str(data.get("service") or "") != "mcp":
        raise ValueError(f"Connection '{conn_id}' is not an MCP connection")
    return sorted(_current_mcp_env(data).keys())


def set_mcp_env_pairs(conn_id: str, *,
                      env_json: Optional[str] = None,
                      env_pairs: Optional[Dict[str, str]] = None,
                      replace: bool = False) -> int:
    """Merge (or replace) env pairs on an MCP connection (encrypted).

    ``env_json`` replaces the whole stored environment with a validated JSON
    object; ``env_pairs`` (plain dict) merges into the stored pairs. When
    ``replace`` is true the stored environment is cleared first. Returns the
    number of stored pairs and drops the cached server client so the next
    call rebuilds the server environment.
    """
    data = _manifest_read(conn_id)
    if str(data.get("service") or "") != "mcp":
        raise ValueError(f"Connection '{conn_id}' is not an MCP connection")
    env_names = get_mcp_env_field_names(conn_id) or None
    stored = {} if replace else _current_mcp_env(data)
    payload: Dict[str, Any] = {}
    if env_json is not None:
        payload["env_json"] = env_json
    if env_pairs is not None:
        payload["env_pairs"] = env_pairs
    normalized = _normalize_mcp_secrets(payload, env_names=env_names)
    if normalized:
        incoming = _mcp_env_json_to_pairs(normalized["env_json"])
        if env_json is not None:
            stored = incoming
        else:
            stored.update(incoming)
    merged = json.dumps(
        stored, ensure_ascii=False, separators=(",", ":")
    ) if stored else ""
    secrets_enc = dict(data.get("secrets_encrypted") or {})
    if merged:
        secrets_enc["env_json"] = encrypt(merged)
        data["credentials_status"] = "active"
    else:
        secrets_enc.pop("env_json", None)
        data["credentials_status"] = "pending"
    data["secrets_encrypted"] = secrets_enc
    data["updated_at"] = _now()
    _manifest_write(conn_id, data)
    try:
        from core.mcp_client import drop_client as _drop_client
        _drop_client(str(conn_id))
    except Exception:
        pass
    return len(stored)


def _memory_path(conn_id: str) -> str:
    """Return the memory.md path for a connection folder."""
    return os.path.join(_connectors_root(), conn_id, CONNECTION_MEMORY_FILE)


def get_connection_memory(conn_id: str) -> str:
    """Return the agent-maintained memory notes of a connection ('' when absent).

    Best effort: read errors yield an empty string so a missing memory file
    never breaks a workflow.
    """
    try:
        with open(_memory_path(conn_id), "r", encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return ""


def set_connection_memory(conn_id: str, content: str) -> bool:
    """Replace the memory notes of a connection. Returns True on success.

    The content is capped at ``CONNECTION_MEMORY_MAX_BYTES``. It must never
    contain credentials - the caller (DevAgent instruction) is responsible
    for keeping secrets out.

    Raises:
        ValueError: when the connection is missing or the content is too large.
    """
    _manifest_read(conn_id)  # validates that the connection exists
    data = str(content if content is not None else "")
    encoded = data.encode("utf-8")
    if len(encoded) > CONNECTION_MEMORY_MAX_BYTES:
        raise ValueError(
            f"Connection memory is too large: {len(encoded)} bytes "
            f"(max {CONNECTION_MEMORY_MAX_BYTES})"
        )
    ensure_dir(os.path.join(_connectors_root(), conn_id))
    with open(_memory_path(conn_id), "w", encoding="utf-8") as f:
        f.write(data)
    return True


def list_services() -> List[Dict[str, Any]]:
    """Return the registry of known connector services."""
    out = []
    for svc_id, svc in CONNECTOR_SERVICES.items():
        entry = dict(svc)
        entry["id"] = svc_id
        out.append(entry)
    return out


def get_service(service: str) -> Optional[Dict[str, Any]]:
    """Return a service registry entry by id, or None."""
    return CONNECTOR_SERVICES.get(_validate_service(service))
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
