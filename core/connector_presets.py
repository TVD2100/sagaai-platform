# -*- coding: utf-8 -*-
"""
core.connector_presets - bundled connection presets (MCP servers and friends).

Presets live under ``defaults/connectors/<service>/<preset_id>.json`` and are
published together with the platform. A preset describes everything except
credentials: name/description, the validated ``config`` (stdlib transport,
HTTP URL, auth-header template, secret-field mapping) and an optional
``memory_seed`` - the initial notes for the connection's memory.md.

A preset may set ``hidden: true``: it disappears from the tile catalogue
(``list_presets``) but remains resolvable by id, so connections created
from it keep their preset fields and the preset can be re-enabled by
clearing the flag.

DevAgent creates connections from presets: ``instantiate_preset`` builds a
draft (``credentials_status=pending``) with the preset config and seeds the
connection memory. The user later fills only the secrets on the Connectors
page or in the chat. Presets never contain secret values.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from core import defaults as defaults_mod


# Snapshot of a preset exposed to tools/UI (no secrets by construction).
_PUBLIC_KEYS = (
    "id", "service", "name", "description", "icon", "config",
    "memory_seed", "sort_order", "hidden",
)

_MAX_NAME_LEN = 120
_MAX_DESCRIPTION_LEN = 300
_MAX_ICON_LEN = 32
_MEMORY_SEED_MAX_BYTES = 60 * 1024


def _preset_root(preset_root: Optional[str] = None) -> str:
    """Return the defaults/connectors root (runtime-resolved for tests)."""
    if preset_root:
        return preset_root
    return defaults_mod.connector_presets_dir()


def _service_dir(service: str, preset_root: Optional[str] = None) -> str:
    return os.path.join(_preset_root(preset_root), service)


def _is_valid_id(value: str) -> bool:
    if not value or len(value) > 64:
        return False
    for ch in value:
        if not (ch.isalnum() or ch in "_-"):
            return False
    return True


def list_preset_ids(service: str = "mcp",
                    preset_root: Optional[str] = None) -> List[str]:
    """Return the sorted preset ids (file names) of a service."""
    root = _service_dir(service, preset_root)
    if not os.path.isdir(root):
        return []
    ids = []
    for name in sorted(os.listdir(root)):
        if name.startswith(".") or not name.endswith(".json"):
            continue
        preset_id = name[: -len(".json")]
        if _is_valid_id(preset_id):
            ids.append(preset_id)
    return ids


def _coerce_hidden(value: Any) -> bool:
    """Return the preset's ``hidden`` flag (unknown values mean visible).

    The flag only removes a preset from the catalogue; the preset stays
    resolvable by id so connections created from it keep their fields.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def _validate_preset(data: Dict[str, Any], preset_id: str,
                     service: str) -> Dict[str, Any]:
    """Validate one preset dict; raises ValueError with a clear message."""
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError(
            f"Preset '{service}/{preset_id}' has no name"
        )
    if len(name) > _MAX_NAME_LEN:
        raise ValueError(
            f"Preset '{service}/{preset_id}' name is too long"
        )
    description = str(data.get("description") or "").strip()
    if len(description) > _MAX_DESCRIPTION_LEN:
        raise ValueError(
            f"Preset '{service}/{preset_id}' description is too long"
        )
    icon = str(data.get("icon") or "").strip()
    if len(icon) > _MAX_ICON_LEN:
        raise ValueError(
            f"Preset '{service}/{preset_id}' icon is too long"
        )
    config = data.get("config")
    if not isinstance(config, dict) or not config:
        raise ValueError(
            f"Preset '{service}/{preset_id}' has no config"
        )
    # The service registry owns config validation; reuse it so preset
    # configs can never diverge from what create_connection accepts.
    if service == "mcp":
        from core.connectors import _normalize_mcp_config
        config = _normalize_mcp_config(config)
    else:
        raise ValueError(f"Unsupported preset service: {service}")
    memory_seed = data.get("memory_seed")
    if memory_seed is not None:
        memory_seed = str(memory_seed)
        if len(memory_seed.encode("utf-8")) > _MEMORY_SEED_MAX_BYTES:
            raise ValueError(
                f"Preset '{service}/{preset_id}' memory_seed is too large"
            )
    else:
        memory_seed = ""
    sort_order = data.get("sort_order")
    try:
        sort_order = int(sort_order) if sort_order not in (None, "") else 100
    except (TypeError, ValueError):
        raise ValueError(
            f"Preset '{service}/{preset_id}' sort_order must be an integer"
        )
    preset = {
        "id": preset_id,
        "service": service,
        "name": name,
        "description": description,
        "icon": icon,
        "config": config,
        "memory_seed": memory_seed,
        "sort_order": sort_order,
        "hidden": _coerce_hidden(data.get("hidden")),
    }
    return preset


def load_preset(preset_id: str, service: str = "mcp",
                preset_root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Load and validate one preset; None when the file is missing/corrupt."""
    preset_id = str(preset_id or "").strip()
    if not _is_valid_id(preset_id):
        return None
    path = os.path.join(
        _service_dir(service, preset_root), f"{preset_id}.json"
    )
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return _validate_preset(data, preset_id, service)
    except ValueError:
        return None


def find_preset(preset_id: str, service: str = "mcp",
                preset_root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Resolve a preset id from the bundled defaults."""
    return load_preset(preset_id, service, preset_root)


def _public_view(data: Dict[str, Any]) -> Dict[str, Any]:
    """White-list a preset dict for public exposure."""
    return {key: data.get(key) for key in _PUBLIC_KEYS if key in data}


def list_presets(service: str = "mcp",
                 preset_root: Optional[str] = None,
                 include_hidden: bool = False) -> List[Dict[str, Any]]:
    """Return the presets of a service (sorted by sort_order, then id).

    Presets marked ``hidden`` are left out of the catalogue unless
    ``include_hidden`` is set: they stay resolvable by id, so connections
    created from them keep their preset fields, but no new tile is offered.
    """
    result = []
    for preset_id in list_preset_ids(service, preset_root):
        preset = load_preset(preset_id, service, preset_root)
        if not preset:
            continue
        if preset.get("hidden") and not include_hidden:
            continue
        result.append(_public_view(preset))
    return sorted(
        result,
        key=lambda p: (int(p.get("sort_order") or 100), str(p.get("id"))),
    )


def resolve_preset(preset_id: str, service: str = "mcp",
                   preset_root: Optional[str] = None,
                   ) -> Optional[Dict[str, Any]]:
    """Resolve a preset id to its public view (None when unknown)."""
    preset = load_preset(preset_id, service, preset_root)
    return _public_view(preset) if preset else None


def instantiate_preset(preset_id: str, service: str = "mcp",
                       name: str = "",
                       preset_root: Optional[str] = None,
                       account: str = "",
                       secrets: Optional[Dict[str, Any]] = None,
                       pending: bool = True,
                       ) -> Dict[str, Any]:
    """Create a connection from a preset.

    With ``pending=True`` (the default) the preset becomes a DRAFT without
    credentials: the user later fills only the credential fields. Passing
    ``pending=False`` creates an active connection in one step - the UI
    collects the credentials in its own form and submits them together
    with the preset, so nothing is written until the user confirms.

    The preset config is validated once more through the connection
    registry and stored on the connection; the preset memory_seed becomes
    the initial memory.md notes (technical facts and guidance - never
    secrets).

    Args:
        preset_id: preset file name (without ``.json``).
        service: connector service the preset belongs to.
        name: display name; empty keeps the preset name.
        preset_root: optional override of the presets root (tests).
        account: optional display account/login info.
        secrets: plaintext secrets for an active create (MCP env pairs).
        pending: True builds a credential-less draft (default).

    Returns ``{"connection": <public manifest>, "preset": <id>}``.

    Raises:
        ValueError: unknown preset, invalid service, or creation failure.
    """
    preset = load_preset(preset_id, service, preset_root)
    if preset is None:
        raise ValueError(
            f"Unknown connection preset '{service}/{preset_id}'"
        )
    from core import connectors
    clean_name = str(name or "").strip() or preset["name"]
    config = dict(preset["config"])
    config["preset"] = preset_id
    connection = connectors.create_connection(
        service, clean_name,
        account=account or "",
        config=config,
        secrets=None if pending else secrets,
        pending=pending,
    )
    conn_id = str(connection.get("id") or "")
    if conn_id and preset.get("memory_seed"):
        try:
            connectors.set_connection_memory(conn_id, preset["memory_seed"])
        except Exception:
            pass  # memory seeding is best effort; the draft stays usable
    return {"connection": connection, "preset": preset_id}
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
