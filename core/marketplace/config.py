"""
core.marketplace.config - marketplace settings stored in the global config.

Settings live in the platform KV config under the ``marketplace.`` prefix so
they follow the same storage, backup and encryption rules as every other
platform setting:

    marketplace.enabled             master switch (default true)
    marketplace.base_url            catalog root (empty = production repo)
    marketplace.allow_code          install executable files (default true)
    marketplace.require_signature   refuse unsigned manifests (default false)
    marketplace.channel             release channel name (default "main")

``allow_code`` defaults to true because the catalog currently holds only
the owner's own entities; the switch exists so a future catalog open to
third parties can be closed without a code change.
"""
from __future__ import annotations

from typing import Any, Dict


PREFIX = "marketplace."

DEFAULTS: Dict[str, Any] = {
    "marketplace.enabled": True,
    "marketplace.base_url": "",
    "marketplace.allow_code": True,
    "marketplace.require_signature": False,
    "marketplace.channel": "main",
}

_TRUE = ("1", "true", "yes", "on")


def _as_bool(value: Any, fallback: bool) -> bool:
    """Coerce a stored config value to bool, falling back on garbage."""
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    text = str(value).strip().lower()
    if not text:
        return fallback
    return text in _TRUE


def _load_raw() -> dict:
    """Return the platform config, or an empty dict when unavailable."""
    try:
        from core.config import load_config

        data = load_config()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def marketplace_settings() -> Dict[str, Any]:
    """Return the effective marketplace settings.

    Every key falls back to :data:`DEFAULTS` when absent or unparsable, so a
    fresh installation works without writing anything to the config first.
    """
    raw = _load_raw()
    return {
        "enabled": _as_bool(raw.get("marketplace.enabled"), DEFAULTS["marketplace.enabled"]),
        "base_url": str(raw.get("marketplace.base_url") or "").strip(),
        "allow_code": _as_bool(raw.get("marketplace.allow_code"), DEFAULTS["marketplace.allow_code"]),
        "require_signature": _as_bool(
            raw.get("marketplace.require_signature"),
            DEFAULTS["marketplace.require_signature"],
        ),
        "channel": str(raw.get("marketplace.channel") or DEFAULTS["marketplace.channel"]).strip()
        or DEFAULTS["marketplace.channel"],
    }


def is_enabled() -> bool:
    """True when the marketplace section is switched on."""
    return bool(marketplace_settings()["enabled"])


def allow_code() -> bool:
    """True when packages may install their executable files."""
    return bool(marketplace_settings()["allow_code"])


def save_settings(**values: Any) -> bool:
    """Persist a subset of marketplace settings.

    Only known keys are written; unknown arguments are ignored so a caller
    from a future version cannot corrupt the config. ``core.config`` owns
    encryption and the KV transaction.
    """
    payload = {}
    for name, value in values.items():
        key = f"{PREFIX}{name}"
        if key not in DEFAULTS:
            continue
        if isinstance(DEFAULTS[key], bool):
            payload[key] = bool(value)
        else:
            payload[key] = "" if value is None else str(value)
    if not payload:
        return False
    try:
        from core.config import load_config, save_config

        config = load_config()
        config.update(payload)
        return bool(save_config(config))
    except Exception:
        return False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
