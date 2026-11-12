# -*- coding: utf-8 -*-
"""
core.connector_presets - connection presets (MCP servers and friends).

Presets live in two roots:

  * ``defaults/connectors/<service>/<preset_id>.json`` - templates shipped
    with the platform;
  * ``DATA_DIR/connector_presets/<service>/<preset_id>.json`` - presets
    installed from the marketplace at run time.

A preset describes everything except credentials: name/description, the
validated ``config`` (transport, HTTP URL, auth-header template,
secret-field mapping) and an optional ``memory_seed`` - the initial notes
for the connection's memory.md.

The user root wins on an id collision: a preset installed from the catalog
replaces the bundled template of the same id while leaving the bundled
file on disk, so removing the installed preset restores the template.

The bundled catalogue is what the MCP tab of the Connectors page offers as
tiles; a preset installed from the catalog lands there too, and a user may
create as many connections from one preset as the accounts they keep.

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
from typing import Any, Dict, List, Optional, Tuple

import core.paths as core_paths
from core import defaults as defaults_mod

# Snapshot of a preset exposed to tools/UI (no secrets by construction).
_PUBLIC_KEYS = (
    "id", "service", "name", "description", "icon", "config",
    "memory_seed", "sort_order", "hidden", "origin",
)

#: Directory under ``DATA_DIR`` holding presets installed from the catalog.
USER_PRESETS_DIRNAME = "connector_presets"

#: Origin values reported for a resolved preset.
ORIGIN_BUNDLED = "bundled"
ORIGIN_USER = "user"

_MAX_NAME_LEN = 120
_MAX_DESCRIPTION_LEN = 300
_MAX_ICON_LEN = 32
_MEMORY_SEED_MAX_BYTES = 60 * 1024


# ─── Roots ────────────────────────────────────────────────────────────────

def _preset_root(preset_root: Optional[str] = None) -> str:
    """Return the bundled presets root (runtime-resolved for tests)."""
    if preset_root:
        return preset_root
    return defaults_mod.connector_presets_dir()


def _data_dir() -> str:
    """Return the live ``DATA_DIR`` (resolved per call, not at import time)."""
    return str(getattr(core_paths, "DATA_DIR", "") or "")


def user_preset_root(service: str = "mcp") -> str:
    """Return the root of presets installed from the catalog for a service.

    The path is resolved on every call: tests point ``core.paths.DATA_DIR``
    at a temporary directory, and a value captured at import time would keep
    writing into the installation.
    """
    return os.path.join(_data_dir(), USER_PRESETS_DIRNAME, service)


def _service_dir(service: str, preset_root: Optional[str] = None) -> str:
    """Return the bundled directory of one service."""
    return os.path.join(_preset_root(preset_root), service)


def _user_service_dir(service: str) -> str:
    """Return the user-level directory of one service."""
    return user_preset_root(service)


def _is_valid_id(value: str) -> bool:
    if not value or len(value) > 64:
        return False
    for ch in value:
        if not (ch.isalnum() or ch in "_-"):
            return False
    return True


# ─── Catalogue ────────────────────────────────────────────────────────────

def _list_ids_in(root: str) -> List[str]:
    """Return the sorted preset ids (file names) found under *root*."""
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


def list_preset_ids(service: str = "mcp",
                    preset_root: Optional[str] = None) -> List[str]:
    """Return the sorted preset ids of one service (bundled root only).

    The user-level root is merged by :func:`list_presets`; this function
    keeps meaning "the ids of one explicit root" so a caller that already
    holds a root never sees a second one mixed in.
    """
    return _list_ids_in(_service_dir(service, preset_root))


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


# ─── Validation ───────────────────────────────────────────────────────────

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


# ─── Loading ──────────────────────────────────────────────────────────────

def _load_file(path: str, preset_id: str, service: str) -> Optional[Dict[str, Any]]:
    """Load and validate one preset file; None when missing/corrupt."""
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


def _load_with_origin(preset_id: str, service: str = "mcp",
                      preset_root: Optional[str] = None
                      ) -> Optional[Tuple[Dict[str, Any], str]]:
    """Return ``(preset, origin)`` for an id, or None when unknown.

    An explicit *preset_root* pins the lookup to that one root (tests and
    callers that already hold a root). Without it the user-level root is
    searched first: a preset installed from the catalog shadows the bundled
    template of the same id without deleting it.
    """
    preset_id = str(preset_id or "").strip()
    if not _is_valid_id(preset_id):
        return None
    if preset_root is not None:
        preset = _load_file(
            os.path.join(_service_dir(service, preset_root), f"{preset_id}.json"),
            preset_id, service,
        )
        return (preset, ORIGIN_BUNDLED) if preset else None
    user_preset = _load_file(
        os.path.join(_user_service_dir(service), f"{preset_id}.json"),
        preset_id, service,
    )
    if user_preset is not None:
        return user_preset, ORIGIN_USER
    bundled = _load_file(
        os.path.join(_service_dir(service, None), f"{preset_id}.json"),
        preset_id, service,
    )
    if bundled is not None:
        return bundled, ORIGIN_BUNDLED
    return None


def load_preset(preset_id: str, service: str = "mcp",
                preset_root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Load and validate one preset; None when the file is missing/corrupt."""
    found = _load_with_origin(preset_id, service, preset_root)
    return found[0] if found else None


def bundled_preset(preset_id: str, service: str = "mcp",
                   preset_root: Optional[str] = None
                   ) -> Optional[Dict[str, Any]]:
    """Return the BUNDLED preset with this id, ignoring the user root.

    An explicit *preset_root* pins the lookup to that root, which is what a
    test needs; without it the shipped ``defaults/connectors`` is read.
    """
    preset_id = str(preset_id or "").strip()
    if not _is_valid_id(preset_id):
        return None
    return _load_file(
        os.path.join(_service_dir(service, preset_root), f"{preset_id}.json"),
        preset_id, service,
    )


def user_preset(preset_id: str, service: str = "mcp",
                ) -> Optional[Dict[str, Any]]:
    """Return the user-level preset with this id, ignoring the bundled root."""
    preset_id = str(preset_id or "").strip()
    if not _is_valid_id(preset_id):
        return None
    return _load_file(
        os.path.join(_user_service_dir(service), f"{preset_id}.json"),
        preset_id, service,
    )


def find_preset(preset_id: str, service: str = "mcp",
                preset_root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Resolve a preset id from the known roots."""
    return load_preset(preset_id, service, preset_root)


def preset_origin(preset_id: str, service: str = "mcp") -> str:
    """Return ``'user'``, ``'bundled'`` or ``''`` for an unknown id."""
    found = _load_with_origin(preset_id, service, None)
    return found[1] if found else ""


def _public_view(data: Dict[str, Any], origin: str) -> Dict[str, Any]:
    """White-list a preset dict for public exposure."""
    view = {key: data.get(key) for key in _PUBLIC_KEYS if key in data}
    view["origin"] = origin
    return view


def list_presets(service: str = "mcp",
                 preset_root: Optional[str] = None,
                 include_hidden: bool = False,
                 include_user: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Return the presets of a service (sorted by sort_order, then id).

    Without an explicit *preset_root* the bundled catalogue and the presets
    installed from the marketplace are merged, the user-level file winning
    on an id collision. *include_user* is derived from that: it is on for
    the live catalogue and off for a caller that pinned one root.

    Presets marked ``hidden`` are left out of the catalogue unless
    ``include_hidden`` is set: they stay resolvable by id, so connections
    created from them keep their preset fields, but no new tile is offered.
    """
    if include_user is None:
        include_user = preset_root is None
    if preset_root is not None:
        include_user = False
    ids: List[str] = []
    for preset_id in list_preset_ids(service, preset_root):
        if preset_id not in ids:
            ids.append(preset_id)
    if include_user:
        for preset_id in _list_ids_in(_user_service_dir(service)):
            if preset_id not in ids:
                ids.append(preset_id)
    result = []
    for preset_id in ids:
        found = _load_with_origin(preset_id, service, preset_root)
        if not found:
            continue
        preset, origin = found
        if preset.get("hidden") and not include_hidden:
            continue
        result.append(_public_view(preset, origin))
    return sorted(
        result,
        key=lambda p: (int(p.get("sort_order") or 100), str(p.get("id"))),
    )


def resolve_preset(preset_id: str, service: str = "mcp",
                   preset_root: Optional[str] = None,
                   ) -> Optional[Dict[str, Any]]:
    """Resolve a preset id to its public view (None when unknown)."""
    found = _load_with_origin(preset_id, service, preset_root)
    return _public_view(found[0], found[1]) if found else None


# ─── Writing the user root ────────────────────────────────────────────────

def save_preset(preset_id: str, data: Dict[str, Any], service: str = "mcp",
                ) -> Dict[str, Any]:
    """Validate a preset and write it into the user-level root.

    This is how a marketplace MCP entity becomes a tile: the catalog supplies
    the template, the platform stores it under ``DATA_DIR`` and the user
    creates connections from the tile like from any bundled preset.

    Returns the public view of the stored preset. Raises ``ValueError`` for
    an unusable id or a preset the connector layer refuses.
    """
    preset_id = str(preset_id or "").strip()
    if not _is_valid_id(preset_id):
        raise ValueError(
            f"Preset id '{preset_id or '-'}' is not usable: [A-Za-z0-9_-], up to 64 chars"
        )
    if not isinstance(data, dict):
        raise ValueError("Preset data must be a JSON object")
    preset = _validate_preset(data, preset_id, service)
    payload: Dict[str, Any] = {
        "name": preset["name"],
        "description": preset["description"],
        "icon": preset["icon"],
        "sort_order": preset["sort_order"],
        "config": preset["config"],
    }
    if preset.get("memory_seed"):
        payload["memory_seed"] = preset["memory_seed"]
    if preset.get("hidden"):
        payload["hidden"] = True
    directory = _user_service_dir(service)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{preset_id}.json")
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    return _public_view(preset, ORIGIN_USER)


def delete_preset(preset_id: str, service: str = "mcp") -> bool:
    """Remove a preset from the user-level root; a bundled one is kept.

    Returns True when a file was removed. The bundled template of the same
    id - if any - becomes visible again on the next catalogue read.
    """
    preset_id = str(preset_id or "").strip()
    if not _is_valid_id(preset_id):
        return False
    path = os.path.join(_user_service_dir(service), f"{preset_id}.json")
    if not os.path.isfile(path):
        return False
    try:
        os.remove(path)
    except OSError:
        return False
    return True


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
