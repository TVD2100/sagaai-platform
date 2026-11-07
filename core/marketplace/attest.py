"""
core.marketplace.attest - signature policy for catalog manifests and installs.

The catalog is untrusted input: a package hash proves the bytes arrived
intact, never that the owner published them. This module is the policy layer
over :mod:`core.signing`:

  * ``signature_keyring`` - where the trusted public keys live (never inside
    the catalog they vouch for);
  * ``verify_signature`` - verify one manifest against that keyring, honouring
    the channel policy (a stable channel or ``require_signature`` refuses an
    unsigned entry, a development channel reports it);
  * ``fetch_signature`` / ``check_manifest`` - the transport side: read the
    ``.sig`` sidecar and verify the bytes before anything parses them;
  * ``install_gate`` - the installer side: refuse before a package download
    when the manifest was not signed by a trusted key.

The split from ``verify.py`` is deliberate: integrity checks (hashes, archive
safety) are one concern, provenance is another, and the policy needs the
marketplace settings plus the signing primitives.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from core import signing

#: Catalog folders are plural while entity types are singular.
TYPE_FOLDERS = {"agent": "agents", "skill": "skills", "rag": "rag", "mcp": "mcp"}


def _settings() -> dict:
    """Return the marketplace settings, or an empty dict when unavailable."""
    try:
        from core.marketplace import config as marketplace_config

        return dict(marketplace_config.marketplace_settings() or {})
    except Exception:  # noqa: BLE001 - defaults below are the conservative ones
        return {}


def signature_keyring(key_dir: Optional[str] = None) -> str:
    """Return the folder of public keys the marketplace trusts.

    The anchors ship inside the installation, never inside a downloaded
    catalog: a key that arrives with the data it is meant to vouch for
    proves nothing. ``marketplace.signature_keyring`` may override the folder
    for a private mirror.
    """
    explicit = str(key_dir or "").strip()
    if explicit:
        return explicit
    configured = str(_settings().get("signature_keyring") or "").strip()
    if configured:
        return configured
    install_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    return os.path.join(install_root, "keys")


def policy_for(channel: Optional[str], settings: Optional[dict] = None) -> dict:
    """Return the channel policy with the marketplace setting applied."""
    source = _settings() if settings is None else settings
    return signing.policy_for(
        channel or source.get("channel"),
        {"require_signature": bool(source.get("require_signature"))},
    )


def verify_signature(
    entity: dict,
    raw_index: Optional[bytes] = None,
    key_dir: Optional[str] = None,
    signature: Optional[dict] = None,
    raw_entity: Optional[bytes] = None,
    channel: Optional[str] = None,
) -> dict:
    """Verify the Ed25519 signature of an entity manifest.

    *raw_entity* is the exact bytes the manifest arrived as - a signature
    covers bytes, not a parsed dict, so the caller passes what it read and
    nothing is re-serialised here. *raw_index* is kept for callers that verify
    an index payload instead.

    The report carries ``verified``/``enforced``/``reason``/``policy``. A
    failure raises :class:`core.marketplace.verify.VerifyError` when the policy
    enforces signatures; otherwise it is only reported. An unsigned manifest
    is never reported as verified.
    """
    from core.marketplace.verify import VerifyError

    policy = policy_for(channel)
    payload = raw_entity if raw_entity is not None else raw_index
    if payload is None:
        outcome = {"ok": False, "reason": "payload_missing", "key_id": None}
    else:
        outcome = signing.verify_bytes(
            payload, signature, signing.load_keyring(signature_keyring(key_dir))
        )
    report = {
        "verified": bool(outcome.get("ok")),
        "enforced": bool(policy.get("enforced")),
        "key_id": outcome.get("key_id"),
        "reason": str(outcome.get("reason") or ""),
        "channel": policy.get("channel"),
        "policy": policy.get("mode"),
        "id": entity.get("id") if isinstance(entity, dict) else None,
    }
    if report["verified"]:
        return report
    if report["enforced"]:
        raise VerifyError(
            "signature_required",
            "The catalog entry is not signed by a trusted key: " + report["reason"],
            report,
        )
    return report


def fetch_signature(fetch_bytes, relative: str) -> Optional[dict]:
    """Return the parsed ``<file>.sig`` sidecar, or None when absent.

    A missing sidecar is not a transport error - whether it is a refusal is
    the channel policy's decision. A sidecar that exists but is not a JSON
    object is reported as malformed rather than silently dropped.
    """
    try:
        raw = fetch_bytes(relative + ".sig")
    except Exception:  # noqa: BLE001 - any transport failure means "no sidecar"
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"malformed": True}
    return record if isinstance(record, dict) else {"malformed": True}


def check_manifest(
    fetch_bytes,
    relative: str,
    raw: bytes,
    entity: dict,
    channel: Optional[str] = None,
) -> dict:
    """Verify a downloaded manifest before anything parses it.

    Returns the verification report. A channel that requires signatures turns
    a failure into a transport error (``signature_refused``), so an unsigned
    or tampered manifest never reaches the JSON parser, a card, or an
    installer.
    """
    from core.marketplace.source import SourceError

    record = fetch_signature(fetch_bytes, relative)
    try:
        return verify_signature(
            entity, signature=record, raw_entity=raw, channel=channel
        )
    except Exception as exc:  # noqa: BLE001 - re-raised as a transport refusal
        details = dict(getattr(exc, "details", {}) or {})
        raise SourceError(
            "signature_refused",
            "Catalog manifest refused: " + str(getattr(exc, "message", exc)),
            details,
        ) from exc


def install_gate(entity: dict, source=None, channel: Optional[str] = None) -> dict:
    """Refuse an install whose manifest was not signed by a trusted key.

    The transport verifies every manifest it downloads and records the report
    on itself; this is the installer-side check. It runs before a package
    download, so an enforcing channel never spends a request on a package
    whose manifest was not signed by a trusted key. On a warning channel the
    report is returned and the install proceeds, so the caller can record that
    the entity is unsigned.
    """
    from core.marketplace.verify import VerifyError

    policy = policy_for(channel)
    reports = getattr(source, "signatures", None) or {}
    type_ = str((entity or {}).get("type") or "skill").strip()
    folder = TYPE_FOLDERS.get(type_, type_)
    entity_id = str((entity or {}).get("id") or "").strip()
    version = str((entity or {}).get("version") or "").strip() or "latest"
    relative = f"{folder}/{entity_id}/{version}/entity.json"
    report = reports.get(relative) or reports.get(
        f"{folder}/{entity_id}/latest/entity.json"
    ) or {}
    outcome = {
        "verified": bool(report.get("verified")),
        "enforced": bool(policy.get("enforced")),
        "key_id": report.get("key_id"),
        "reason": str(report.get("reason") or "signature_missing"),
        "channel": policy.get("channel"),
        "policy": policy.get("mode"),
        "path": relative,
    }
    if outcome["verified"]:
        return outcome
    if outcome["enforced"]:
        raise VerifyError(
            "signature_required",
            "Refusing to install an entity without a trusted signature: "
            + outcome["reason"],
            outcome,
        )
    return outcome
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
