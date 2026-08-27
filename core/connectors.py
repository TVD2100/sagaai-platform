# -*- coding: utf-8 -*-
"""
core.connectors - CRUD for external service connections.

Each connection lives in ``DATA_DIR/connectors/<id>/manifest.json``:

    {
        "id": "<uuid8>",
        "service": "github",
        "name": "GitHub-TVD2100",        # user-visible name
        "account": "TVD2100",             # display info about the account
        "token_encrypted": "<fernet>",    # token encrypted with core.crypto
        "created_at": "...",
        "updated_at": "..."
    }

The token is never stored in plain text inside the manifest. Write functions
accept a plaintext token and encrypt it via ``core.crypto.encrypt`` before
persisting. Read functions never return the token; they expose a masked
view (``token_masked``) for the UI and provide ``decrypt_token`` for the
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
    "github": {
        "name": "GitHub",
        "description": "GitHub API connection (repositories, files, issues)",
    },
}

_VALID_ID_RE = re.compile(r"^[a-z0-9_-]+$")


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


def public_manifest(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a manifest dict with secrets removed, suitable for the UI/API.

    Includes ``token_masked`` = "***" when an encrypted token is present,
    plus the raw ``has_token`` boolean.
    """
    out = dict(data)
    out.pop("token_encrypted", None)
    token = data.get("token_encrypted", "")
    out["has_token"] = bool(token)
    out["token_masked"] = "***" if token else ""
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


def create_connection(service: str, name: str, token: str,
                      account: str = "") -> Dict[str, Any]:
    """Create a new connection.

    Args:
        service: connector service id ("github").
        name: user-visible name (e.g. "GitHub-TVD2100").
        token: plaintext API token; it is encrypted before storage.
        account: display account/login info, optional.

    Returns the created public manifest.
    """
    svc = _validate_service(service)
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Connection name cannot be empty")
    token = token or ""
    if not token.strip():
        raise ValueError("Token cannot be empty")

    now = _now()
    conn_id = _unique_conn_id()
    manifest = {
        "id": conn_id,
        "service": svc,
        "name": clean_name,
        "account": (account or "").strip(),
        "token_encrypted": encrypt(token.strip()),
        "created_at": now,
        "updated_at": now,
    }
    _manifest_write(conn_id, manifest)
    return public_manifest(manifest)


def update_connection(conn_id: str, name: str = "", account: str = "",
                      token: str = "") -> Dict[str, Any]:
    """Update a connection's display fields and optionally replace its token.

    Args:
        conn_id: id of the connection to update.
        name: new display name (empty = keep).
        account: new account display string (empty = keep unless token set).
        token: when non-empty, replaces the stored encrypted token.

    Returns the updated public manifest.
    """
    data = _manifest_read(conn_id)
    if (name or "").strip():
        data["name"] = name.strip()
    if (account or "").strip():
        data["account"] = account.strip()
    if (token or "").strip():
        data["token_encrypted"] = encrypt(token.strip())
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
