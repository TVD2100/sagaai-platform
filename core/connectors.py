# -*- coding: utf-8 -*-
"""
core.connectors - CRUD for external service connections.

Each connection lives in ``DATA_DIR/connectors/<id>/manifest.json``:

    {
        "id": "<uuid8>",
        "service": "github_rest" | "ssh",
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
    },
    "ssh": {
        "name": "SSH Server",
        "description": "SSH/SFTP connection to a remote server (paramiko): run commands, list/read/write files",
        "config_fields": ["host", "port", "username"],
        "secret_fields": ["password", "private_key", "key_passphrase"],
    },
}

_VALID_ID_RE = re.compile(r"^[a-z0-9_-]+$")

# Secret field names inside a connection manifest (password, private_key, ...).
_SECRET_FIELD_RE = re.compile(r"^[a-z0-9_]+$")


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
    integer in 1..65535. Other services have no declared config fields and
    yield an empty dict.
    """
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


def _normalize_secrets(service: str, secrets: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Validate secret fields of a connection; unknown fields are rejected.

    Only non-empty values are kept, so empty inputs mean "keep existing".
    """
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
                      secrets: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create a new connection.

    Args:
        service: connector service id ("github_rest", "ssh").
        name: user-visible name (e.g. "GitHub-TVD2100").
        token: plaintext API token (token-style services such as
            github_rest); encrypted before storage.
        account: display account/login info, optional.
        config: non-secret settings of the service (SSH: host, port,
            username).
        secrets: per-field plaintext secrets (SSH: password, private_key,
            key_passphrase); each value is encrypted before storage.

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
    if svc == "ssh":
        normalized_secrets = _normalize_secrets(svc, secrets)
        _validate_ssh_auth(normalized_secrets)
        manifest["config"] = _normalize_config(svc, config)
        manifest["secrets_encrypted"] = {
            key: encrypt(val) for key, val in normalized_secrets.items()
        }
    else:
        token = token or ""
        if not token.strip():
            raise ValueError("Token cannot be empty")
        manifest["token_encrypted"] = encrypt(token.strip())
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
        config: non-secret settings to merge (SSH: host, port, username).
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
    data["updated_at"] = _now()
    _manifest_write(conn_id, data)
    return public_manifest(data)


def set_connection_token(conn_id: str, token: str) -> bool:
    """Replace the encrypted token for a connection. Returns True on success."""
    if not (token or "").strip():
        raise ValueError("Token cannot be empty")
    data = _manifest_read(conn_id)
    data["token_encrypted"] = encrypt(token.strip())
    data["updated_at"] = _now()
    _manifest_write(conn_id, data)
    return True


def delete_connection(conn_id: str) -> bool:
    """Delete a connection folder. Returns True on success."""
    d = os.path.join(_connectors_root(), conn_id)
    try:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        return True
    except Exception:
        return False


def decrypt_token(conn_id: str) -> str:
    """Decrypt and return the plaintext token for a connection.

    Raises:
        ValueError: if the connection is missing or decryption fails.
    """
    data = _manifest_read(conn_id)
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
    secrets_enc = dict(data.get("secrets_encrypted") or {})
    secrets_enc[field_name] = encrypt(val)
    data["secrets_encrypted"] = secrets_enc
    data["updated_at"] = _now()
    _manifest_write(conn_id, data)
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
# SPDX-License-Identifier: MIT
