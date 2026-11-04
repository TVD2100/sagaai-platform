"""
core.api_token_store - the bearer tokens of the HTTP API.

One token per installation in v1, stored as a SHA-256 record in the
``config_kv`` table. Three decisions are deliberate:

- **The token itself is never stored.** A Fernet-encrypted token is
  recoverable by anyone who gets both the database and the key file; a hash
  is not. The cost is honest: the value is shown once, at generation, and
  can only be replaced afterwards.
- **Only the ``api_tokens`` key is read here.** ``core.config.load_config``
  decrypts every provider key of the installation; doing that on each HTTP
  request would put a Fernet operation in front of every call.
- **Writing stays in the UI process.** ``repo_save_config`` replaces the
  whole settings table; two processes writing it lose each other's keys.

The record shape leaves room for several tokens without a migration:

    {"id": "3f9a1c02",          # first 8 hex of the hash, for logs
     "sha256": "<64 hex>",
     "slugs": ["dev_agent"],     # scope; ["*"] = every employee
     "created_at": "2026-11-04T20:15:00Z",
     "expires_at": "",           # empty = no expiry
     "note": "n8n"}
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("core.api_token_store")

#: Key of the settings table that holds the token records.
CONFIG_KEY = "api_tokens"
#: Master switch of the HTTP API.
ENABLED_KEY = "api_enabled"
#: Bind configuration keys.
HOST_KEY = "api_bind_host"
PORT_KEY = "api_bind_port"
#: Second confirmation required for a non-loopback bind.
REMOTE_KEY = "api_allow_remote"

#: Prefix of every token, so a leaked string is recognisable as a SagaAI
#: credential by any secret scanner.
TOKEN_PREFIX = "saga_"
#: Bytes of entropy in a token. 32 bytes = 256 bits: guessing is not a threat
#: model, leaking is.
TOKEN_BYTES = 32
#: Records one installation may hold. A ceiling keeps the verify loop bounded.
MAX_TOKENS = 16
#: Loopback is the only bind that needs no extra confirmation.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost", "")


# ─── Record helpers ─────────────────────────────────────────────────────────


def hash_token(token: str) -> str:
    """Return the hex SHA-256 of a presented token."""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def token_id(sha256: str) -> str:
    """Return the short public identifier of a token record."""
    return str(sha256 or "")[:8]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def generate_token(slugs: Optional[List[str]] = None,
                   note: str = "", expires_at: str = "") -> Dict[str, Any]:
    """Create one token record; the plaintext is returned exactly once.

    The caller is responsible for showing it and for saving the record -
    this function touches no storage, so it can be used in a test without a
    database.
    """
    raw = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    digest = hash_token(raw)
    scope = [str(s).strip() for s in (slugs or ["*"]) if str(s).strip()]
    record = {
        "id": token_id(digest),
        "sha256": digest,
        "slugs": scope,
        "created_at": _now_iso(),
        "expires_at": str(expires_at or ""),
        "note": str(note or ""),
    }
    return {"token": raw, "record": record}


def verify_token(presented: str,
                 records: Optional[List[Dict[str, Any]]] = None
                 ) -> Optional[Dict[str, Any]]:
    """Return the matching record for *presented*, or None.

    The comparison is constant-time per record and every record is checked:
    returning early on the first mismatch would make the answer depend on a
    record's position in the list.
    """
    text = str(presented or "").strip()
    if not text or not text.startswith(TOKEN_PREFIX):
        return None
    digest = hash_token(text)
    match = None
    for record in records or []:
        if not isinstance(record, dict):
            continue
        stored = str(record.get("sha256") or "")
        if not stored:
            continue
        if secrets.compare_digest(stored, digest):
            match = record
    return match


def is_expired(record: Dict[str, Any], now: Optional[float] = None) -> bool:
    """Return True when a record carries a passed ``expires_at``.

    An empty or unparsable value means "no expiry": refusing to parse a
    date must not silently disable a working token, and the field is filled
    from a date picker.
    """
    if not isinstance(record, dict):
        return False
    raw = str(record.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        stamp = raw.replace("Z", "+00:00")
        deadline = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    reference = datetime.fromtimestamp(now, tz=timezone.utc) if now else \
        datetime.now(timezone.utc)
    return reference > deadline


def scope_of(record: Dict[str, Any]) -> List[str]:
    """Return the slug scope of a token record.

    A record without a usable scope gets ``[]`` - nothing - because a missing
    field must never widen access.
    """
    raw = record.get("slugs") if isinstance(record, dict) else None
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


# ─── Storage access ─────────────────────────────────────────────────────────


def load_settings() -> Dict[str, Any]:
    """Return the API settings section of the stored configuration.

    Reads the raw settings row and picks five keys; it deliberately does not
    go through ``core.config.load_config``, which decrypts every secret of
    the installation on each call.
    """
    try:
        from storage.repository import repo_load_config
        raw = repo_load_config() or {}
    except Exception as exc:
        log.warning("api settings read failed: %s", exc)
        raw = {}
    return {
        ENABLED_KEY: bool(raw.get(ENABLED_KEY, False)),
        HOST_KEY: str(raw.get(HOST_KEY) or "127.0.0.1"),
        PORT_KEY: int(raw.get(PORT_KEY) or 8765),
        REMOTE_KEY: bool(raw.get(REMOTE_KEY, False)),
    }


def load_records() -> List[Dict[str, Any]]:
    """Return the stored token records, newest first."""
    try:
        from storage.repository import repo_load_config
        raw = repo_load_config() or {}
    except Exception as exc:
        log.warning("token records read failed: %s", exc)
        return []
    value = raw.get(CONFIG_KEY)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def save_records(records: List[Dict[str, Any]]) -> bool:
    """Persist the token records; the UI process is the only caller.

    Saving goes through ``core.config.save_config`` so the rest of the
    settings survive: the repository call behind it replaces the whole
    table, and passing five keys alone would delete every other one.
    """
    try:
        from core.config import load_config, save_config
        config = load_config() or {}
        config[CONFIG_KEY] = list(records)[:MAX_TOKENS]
        return bool(save_config(config))
    except Exception as exc:
        log.warning("token records save failed: %s", exc)
        return False


def add_record(record: Dict[str, Any]) -> bool:
    """Append one record, dropping the oldest when the ceiling is reached."""
    records = load_records()
    records.insert(0, dict(record))
    return save_records(records[:MAX_TOKENS])


def revoke(record_id: str) -> bool:
    """Remove one record by its public id."""
    key = str(record_id or "").strip()
    if not key:
        return False
    records = [r for r in load_records()
               if str(r.get("id") or "") != key]
    return save_records(records)


def is_remote_bind(host: str) -> bool:
    """Return True when *host* is not a loopback address."""
    return str(host or "").strip() not in LOOPBACK_HOSTS


def api_state() -> Dict[str, Any]:
    """Return the effective state of the API switch in one call.

    ``available`` is the single question a transport asks before serving a
    request: is the API on AND does a usable, unexpired token exist? A
    ``True`` here with no token is exactly the fail-open hole this function
    exists to close.
    """
    settings = load_settings()
    records = load_records()
    usable = [r for r in records if not is_expired(r)]
    return {
        "enabled": bool(settings[ENABLED_KEY]),
        "host": settings[HOST_KEY],
        "port": settings[PORT_KEY],
        "allow_remote": bool(settings[REMOTE_KEY]),
        "token_count": len(records),
        "available": bool(settings[ENABLED_KEY]) and bool(usable),
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
