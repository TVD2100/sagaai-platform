"""
core.marketplace.mcp_installer - installation of an ``mcp`` entity.

An MCP entity ships no files: it is a connector preset - the transport
config, the description of the credential fields and a memory seed.
Installing it therefore does not create a connection. It makes the preset
available on the MCP servers tab of the Connectors page, and the user
creates as many connections from it as the accounts they keep - exactly
like from a bundled preset.

Two properties decide the shape of this module:

  * identity is the preset id. The catalog template is written to the
    user-level preset root (``DATA_DIR/connector_presets/mcp``) unless the
    platform already ships the very same server under that id; the registry
    row records the preset id in ``runtime_slug``, so an update and a
    removal find it without guessing.
  * the catalog can never carry a credential. A preset declares
    ``secret_env_fields`` - names and labels of the fields the user fills
    in - and the installer refuses a preset that carries a value for any of
    them. Credentials are entered when a connection is created from the
    preset, never at install time.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from core.marketplace import config as marketplace_config
from core.marketplace import registry, store
from core.marketplace import source as source_mod
from core.marketplace.installer import (
    InstallError,
    _display_name,
    _licence_state,
    _platform_compatible,
)

#: Connector service an MCP entity belongs to.
DEFAULT_SERVICE = "mcp"

#: Config keys that would carry a credential if an author put one there.
_SECRET_KEY_HINTS = (
    "token",
    "password",
    "passphrase",
    "secret",
    "api_key",
    "apikey",
    "private_key",
    "auth",
)

#: Charset a preset id may use.
#:
#: Lowercase only: the id becomes ``config.preset`` of every connection
#: created from it, and the connector layer accepts ``[a-z0-9_-]`` there.
#: An id the connector layer would refuse must be refused here, where the
#: message can still name the catalog entity.
_PRESET_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

#: Keys of a preset config that are presentation, not part of the server.
_CONFIG_META_KEYS = ("preset", "description")


# ─── Identity ─────────────────────────────────────────────────────────────

def preset_of(entity: dict) -> dict:
    """Return the ``preset`` block of an entity, or an empty dict."""
    block = entity.get("preset")
    if isinstance(block, dict):
        return dict(block)
    # A catalog may inline the preset fields instead of nesting them.
    nested = entity.get("connector") if isinstance(entity.get("connector"), dict) else None
    return dict(nested) if nested else {}


def preset_id_of(entity: dict) -> str:
    """Return the preset id an entity installs, or an empty string.

    The id doubles as the registry ``runtime_slug``: it is what an update
    and a removal look up, and what the MCP tab shows in the connection's
    ``config.preset``.
    """
    block = preset_of(entity)
    for candidate in (
        entity.get("preset_id"),
        block.get("id"),
        entity.get("slug"),
        entity.get("id"),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def service_of(entity: dict) -> str:
    """Return the connector service of an entity (``mcp`` by default)."""
    block = preset_of(entity)
    return str(
        entity.get("service") or block.get("service") or DEFAULT_SERVICE
    ).strip() or DEFAULT_SERVICE


# ─── Validation ───────────────────────────────────────────────────────────

def _secret_leaks(config: dict) -> List[str]:
    """Return config keys that look like they carry a credential value.

    The catalog declares *which* credentials a server needs; it must never
    carry their values. Only the shape is checked here - a key named like a
    credential with a non-empty scalar value - because a published preset is
    reviewed before it reaches the catalog and this check is the safety net,
    not the review.
    """
    leaks: List[str] = []
    for raw_key, raw_value in (config or {}).items():
        key = str(raw_key or "").strip()
        lowered = key.lower()
        if key == "secret_env_fields" or key == "auth_header":
            # ``auth_header`` is a template with ``{ENV}`` placeholders, and
            # ``secret_env_fields`` holds field descriptions, not values.
            continue
        if not any(hint in lowered for hint in _SECRET_KEY_HINTS):
            continue
        if isinstance(raw_value, (dict, list)):
            continue
        if str(raw_value or "").strip():
            leaks.append(key)
    declared = config.get("secret_env_fields")
    if isinstance(declared, dict):
        for field, descriptor in declared.items():
            if not isinstance(descriptor, dict):
                continue
            for value_key in ("value", "default", "secret"):
                if str(descriptor.get(value_key) or "").strip():
                    leaks.append(f"secret_env_fields.{field}.{value_key}")
    return sorted(leaks)


def _auth_header_envs(template: str) -> List[str]:
    """Return the ``{ENV}`` placeholder names of an auth header template."""
    return sorted({match for match in re.findall(r"\{([A-Za-z0-9_]+)\}", template or "")})


def check_protocol(entity: dict) -> None:
    """Refuse a preset whose server cannot speak the platform's revision.

    The platform implements MCP revision ``2025-03-26`` and requires strict
    equality (D-13). A preset declares the revision of its server and,
    optionally, the range it accepts; a server that cannot meet the platform
    is rejected here, where the message can name both revisions, instead of
    failing at the first tool call.
    """
    from core.mcp_client import PROTOCOL_VERSION

    block = preset_of(entity)
    declared = str(block.get("protocol_version") or "").strip()
    compat = block.get("protocol_compat") if isinstance(block.get("protocol_compat"), dict) else {}
    minimum = str(compat.get("min") or "").strip()
    maximum = str(compat.get("max") or "").strip()
    if not declared and not minimum and not maximum:
        return
    if minimum and PROTOCOL_VERSION < minimum:
        raise InstallError(
            "protocol_incompatible",
            f"The server requires MCP {minimum} or newer, the platform speaks {PROTOCOL_VERSION}",
            {"id": str(entity.get("id") or ""), "min": minimum, "platform": PROTOCOL_VERSION},
        )
    if maximum and PROTOCOL_VERSION > maximum:
        raise InstallError(
            "protocol_incompatible",
            f"The server accepts MCP up to {maximum}, the platform speaks {PROTOCOL_VERSION}",
            {"id": str(entity.get("id") or ""), "max": maximum, "platform": PROTOCOL_VERSION},
        )
    if declared and declared != PROTOCOL_VERSION and not (minimum or maximum):
        raise InstallError(
            "protocol_incompatible",
            f"The server speaks MCP {declared}, the platform speaks {PROTOCOL_VERSION}",
            {"id": str(entity.get("id") or ""), "declared": declared, "platform": PROTOCOL_VERSION},
        )


def preset_config(entity: dict) -> Dict[str, Any]:
    """Return the validated connector config an entity installs.

    The config is the transport part of the preset: the same shape
    ``create_connection`` accepts when a connection is later created from
    the tile.
    """
    block = preset_of(entity)
    config = block.get("config") if isinstance(block.get("config"), dict) else {}
    if not config:
        raise InstallError(
            "invalid_preset",
            "The MCP entity declares no connector config",
            {"id": str(entity.get("id") or "")},
        )
    leaks = _secret_leaks(config)
    if leaks:
        raise InstallError(
            "preset_carries_secrets",
            "The catalog preset carries credential values; only field descriptions are allowed",
            {"id": str(entity.get("id") or ""), "fields": leaks[:10]},
        )

    from core.connectors import _normalize_mcp_config

    merged = dict(config)
    # The documented place for the credential field descriptors is inside
    # ``config``, next to the transport they belong to. Some published
    # presets declare them one level up, on the preset itself; lifting them
    # into the config keeps such a preset installable instead of refusing a
    # server whose credentials the user can still fill in.
    if not merged.get("secret_env_fields") and block.get("secret_env_fields"):
        merged["secret_env_fields"] = block.get("secret_env_fields")
    preset_id = preset_id_of(entity)
    if not _PRESET_ID_RE.match(preset_id or ""):
        raise InstallError(
            "invalid_preset_id",
            f"Preset id '{preset_id or '-'}' is not usable: [a-z0-9_-], up to 64 chars",
            {"id": str(entity.get("id") or "")},
        )
    description = str(block.get("description") or "").strip()
    if description and not merged.get("description"):
        merged["description"] = description
    try:
        normalized = _normalize_mcp_config(merged)
    except ValueError as exc:
        raise InstallError(
            "invalid_preset",
            f"The preset config was refused by the connector layer: {exc}",
            {"id": str(entity.get("id") or "")},
        ) from exc

    declared_envs = {
        str(item.get("env") or "")
        for item in (normalized.get("secret_env_fields") or {}).values()
        if isinstance(item, dict) and item.get("env")
    }
    unresolved = [
        name for name in _auth_header_envs(str(normalized.get("auth_header") or ""))
        if name not in declared_envs
    ]
    if unresolved:
        raise InstallError(
            "undeclared_credential_field",
            "The auth header refers to credential fields the preset does not declare",
            {"id": str(entity.get("id") or ""), "fields": unresolved},
        )
    return normalized


# ─── The stored preset ────────────────────────────────────────────────────

def _preset_name(entity: dict, block: dict, preset_id: str) -> str:
    """Return the tile name of a preset.

    The preset block names the SERVER - that is what the MCP tab shows on
    its tile - while the entity name is the card title. The server name
    wins, and the card title is the fallback for a catalog that states only
    one of the two.
    """
    from core.naming import localized_text
    from core.naming_runtime import current_lang

    name = localized_text(block.get("name"), current_lang())
    return name or _display_name(entity) or preset_id


def preset_payload(entity: dict, config: dict) -> Dict[str, Any]:
    """Build the preset file an entity installs.

    The payload is what ``connector_presets.save_preset`` writes and what a
    comparison against the bundled template reads: the presentation fields,
    the server config and the memory seed. ``config.preset`` and
    ``config.description`` are dropped - the first is added by the
    connection layer when the tile is used, the second lives one level up.
    """
    block = preset_of(entity)
    preset_id = preset_id_of(entity)
    clean_config = {
        key: value for key, value in (config or {}).items()
        if key not in _CONFIG_META_KEYS
    }
    payload: Dict[str, Any] = {
        "name": _preset_name(entity, block, preset_id),
        "description": str(block.get("description") or "").strip(),
        "icon": str(block.get("icon") or "").strip(),
        "sort_order": block.get("sort_order") or 100,
        "config": clean_config,
    }
    memory_seed = str(block.get("memory_seed") or "")
    if memory_seed:
        payload["memory_seed"] = memory_seed
    return payload


def _server_signature(config: dict) -> tuple:
    """Return what identifies the SERVER a preset config points at.

    Transport, endpoint, whitelisted command and the header template say
    which server is meant; the names of the credential fields say which
    secrets it needs. Labels, help texts and ordering are presentation and
    stay out of the signature, so a catalog entry that only rewrites a label
    is recognised as the same server instead of shadowing a bundled tile.
    """
    cfg = config if isinstance(config, dict) else {}
    envs = ()
    declared = cfg.get("secret_env_fields")
    if isinstance(declared, dict):
        envs = tuple(sorted(
            str(item.get("env") or "")
            for item in declared.values()
            if isinstance(item, dict)
        ))
    args = cfg.get("args")
    return (
        str(cfg.get("transport") or ""),
        str(cfg.get("url") or ""),
        str(cfg.get("command") or ""),
        tuple(str(arg) for arg in args) if isinstance(args, list) else (),
        str(cfg.get("auth_header") or ""),
        envs,
    )


def _serves_the_same_server(existing: dict, payload: dict) -> bool:
    """True when *existing* describes the server *payload* describes."""
    if not isinstance(existing, dict):
        return False
    return _server_signature(existing.get("config") or {}) == _server_signature(
        payload.get("config") or {}
    )


def _resolve_origin(preset_id: str, payload: dict) -> str:
    """Return ``'bundled'`` when the platform already ships this server.

    A catalog entity that describes a server the platform bundles needs no
    user-level copy: the tile is already there. A different server config
    under a bundled id is a real update of the template, and it is stored
    in the user root, which shadows the bundled file.
    """
    from core import connector_presets

    bundled = connector_presets.bundled_preset(preset_id, DEFAULT_SERVICE)
    if bundled is not None and _serves_the_same_server(bundled, payload):
        return connector_presets.ORIGIN_BUNDLED
    return connector_presets.ORIGIN_USER


def _sync_user_preset(preset_id: str, payload: dict, origin: str) -> bool:
    """Bring the user-level preset root in line with *origin*.

    Returns True when a file was written or removed, so the caller can tell
    a real change from a re-install of what is already there.
    """
    from core import connector_presets

    current = connector_presets.user_preset(preset_id, DEFAULT_SERVICE)
    if origin == connector_presets.ORIGIN_BUNDLED:
        # The catalog now points at the bundled template; a leftover user
        # copy would shadow it and hide the bundled update.
        return bool(current is not None and connector_presets.delete_preset(
            preset_id, DEFAULT_SERVICE))
    if current is not None and _serves_the_same_server(current, payload):
        return False
    try:
        connector_presets.save_preset(preset_id, payload, DEFAULT_SERVICE)
    except ValueError as exc:
        raise InstallError(
            "preset_write_failed",
            f"The preset could not be stored: {exc}",
            {"preset": preset_id},
        ) from exc
    return True


# ─── Install ──────────────────────────────────────────────────────────────

def _mcp_gate(
    entity: dict,
    *,
    accept_license: bool,
) -> dict:
    """Validate an MCP install request and return the shared values.

    Everything that can refuse the run happens here, before the lock is
    taken: the marketplace switch, the protocol revision, the preset fields
    and the platform bounds. The revision check comes first - a server the
    platform cannot talk to is refused for that reason, not for a field of a
    preset it will never use.
    """
    if not isinstance(entity, dict) or not entity.get("id"):
        raise InstallError("invalid_entity", "Entity manifest has no id")

    settings = marketplace_config.marketplace_settings()
    if not settings.get("enabled"):
        raise InstallError(
            "marketplace_disabled",
            "The marketplace is disabled in the settings",
        )

    entity_id = str(entity["id"])
    version = str(entity.get("version") or "0.0.0")
    service = service_of(entity)
    preset_id = preset_id_of(entity)
    check_protocol(entity)
    config = preset_config(entity)

    compatible, reason = _platform_compatible(entity)
    if not compatible:
        raise InstallError(
            "platform_incompatible",
            reason or "Entity is not compatible with this platform build",
            {"id": entity_id, "version": version},
        )

    return {
        "entity_id": entity_id,
        "version": version,
        "service": service,
        "preset_id": preset_id,
        "config": config,
        "licence_at": _licence_state(entity, accept_license),
        "existing_row": registry.get_installed("mcp", entity_id),
    }


def _register_mcp(
    *,
    entity: dict,
    entity_id: str,
    preset_id: str,
    version: str,
    channel: str,
    install_id: str,
    licence_at: Optional[str],
) -> None:
    """Write the registry row of a server - the commit marker of the run."""
    now = store.isoformat_utc()
    registry.upsert_installed(
        {
            "type": "mcp",
            "id": entity_id,
            "runtime_slug": preset_id,
            "runtime_db_id": None,
            "runtime_skill_id": None,
            "fork_of": entity.get("fork_of"),
            "version": version,
            "channel": channel,
            "source_url": str(entity.get("source_url") or ""),
            "package_sha256": None,
            "tree_sha256": registry.manifest_sha256({}),
            "index_sha256": None,
            "files_json": {},
            "skipped_code_files_json": [],
            "installed_at": now,
            "updated_at": now,
            "installed_by": "marketplace",
            "user_modified": False,
            "user_modified_files_json": [],
            "install_id": install_id,
            "bundle_id": entity.get("bundle_id"),
            "pinned": False,
            "license_accepted_at": licence_at,
        }
    )


def _mcp_report(
    *,
    noop: bool,
    entity_id: str,
    version: str,
    install_id,
    preset_id: str,
    service: str,
    origin: str,
    stored: bool,
) -> dict:
    """Build the install report shared by the noop and the success path."""
    return {
        "ok": True,
        "noop": bool(noop),
        "id": entity_id,
        "type": "mcp",
        "version": version,
        "install_id": install_id,
        "runtime_slug": preset_id,
        "preset": preset_id,
        "service": service,
        "origin": origin,
        "stored": bool(stored),
    }


def install_mcp(
    entity: dict,
    *,
    channel: str,
    allow_code: Optional[bool] = None,
    source: Optional[source_mod.MarketplaceSource] = None,
    accept_license: bool = False,
) -> dict:
    """Install one MCP server from the catalog and return a report.

    Installation means the preset becomes available on the MCP servers tab:
    the catalog template is stored under ``DATA_DIR`` unless the platform
    already bundles that very server, and the registry row records the
    preset id. No connection is created - the user makes those from the
    tile, as many as needed, and enters the credentials there.

    Parameters:
        entity: parsed ``entity.json`` of the server.
        channel: release channel recorded on the registry row.
        allow_code: unused (an MCP entity ships no files); accepted so every
            installer shares one call signature.
        source: catalog transport; unused, kept for the same reason.
        accept_license: explicit licence acceptance when required.

    Returns a report with ``preset``, ``origin`` and ``stored``. A re-install
    of the same version is a no-op unless the shipped preset differs from
    the stored one - that difference is what an update of an MCP entity
    means, and it is applied in place.
    """
    gate = _mcp_gate(entity, accept_license=accept_license)
    entity_id = gate["entity_id"]
    version = gate["version"]
    service = gate["service"]
    preset_id = gate["preset_id"]
    config = gate["config"]
    licence_at = gate["licence_at"]
    existing_row = gate["existing_row"]

    payload = preset_payload(entity, config)
    origin = _resolve_origin(preset_id, payload)

    with store.install_lock():
        install_id = store.new_install_id()
        store.append_run(
            {
                "install_id": install_id,
                "type": "mcp",
                "id": entity_id,
                "version": version,
                "action": "install",
                "status": "applying",
            }
        )
        previous_preset = _user_preset_snapshot(preset_id)
        try:
            stored = _sync_user_preset(preset_id, payload, origin)

            # A re-install of what is already there touches nothing: the
            # showcase renders such an entity as skipped, and a run that
            # changed no setting must not look like a fresh install.
            unchanged = (
                existing_row is not None
                and not stored
                and str(existing_row.get("version") or "") == version
            )
            if unchanged:
                return _mcp_report(
                    noop=True,
                    entity_id=entity_id,
                    version=version,
                    install_id=existing_row.get("install_id"),
                    preset_id=preset_id,
                    service=service,
                    origin=origin,
                    stored=False,
                )

            _register_mcp(
                entity=entity,
                entity_id=entity_id,
                preset_id=preset_id,
                version=version,
                channel=channel,
                install_id=install_id,
                licence_at=licence_at,
            )
            store.update_run(
                install_id,
                status="done",
                finished_at=store.isoformat_utc(),
                runtime_slug=preset_id,
            )
            store.clear_state()
            try:
                registry.unmark_removed("mcp", entity_id)
            except Exception:
                pass
            registry.export_registry_cache()
            return _mcp_report(
                noop=False,
                entity_id=entity_id,
                version=version,
                install_id=install_id,
                preset_id=preset_id,
                service=service,
                origin=origin,
                stored=stored,
            )
        except Exception as exc:
            if stored:
                _restore_user_preset(preset_id, previous_preset)
            # The registry row is written last, so a failure before it leaves
            # the previous row - if any - as the truth. Restoring it instead
            # of deleting the id keeps an installed MCP server installed when
            # an update of it fails halfway.
            try:
                if existing_row:
                    registry.upsert_installed(existing_row)
                else:
                    registry.remove_installed("mcp", entity_id)
            except Exception:
                pass
            store.update_run(
                install_id,
                status="failed",
                failed_at=store.isoformat_utc(),
                failure_reason=str(exc),
            )
            store.clear_state()
            if isinstance(exc, InstallError):
                raise
            if isinstance(exc, store.MarketplaceError):
                raise InstallError(exc.code, exc.message, exc.details) from exc
            raise InstallError(
                "install_failed",
                f"MCP server installation failed: {exc}",
                {"id": entity_id, "version": version},
            ) from exc


def _user_preset_snapshot(preset_id: str) -> Optional[dict]:
    """Return the raw user-level preset before a run changes it."""
    from core import connector_presets

    return connector_presets.user_preset(preset_id, DEFAULT_SERVICE)


def _restore_user_preset(preset_id: str, previous: Optional[dict]) -> None:
    """Undo the preset write of a failed run.

    A preset that was not there before is removed; one that was there is
    written back. Either way the failure leaves the MCP tab as it was.
    """
    from core import connector_presets

    try:
        if previous is None:
            connector_presets.delete_preset(preset_id, DEFAULT_SERVICE)
            return
        connector_presets.save_preset(
            preset_id,
            {
                "name": previous.get("name") or preset_id,
                "description": previous.get("description") or "",
                "icon": previous.get("icon") or "",
                "sort_order": previous.get("sort_order") or 100,
                "config": dict(previous.get("config") or {}),
                "memory_seed": previous.get("memory_seed") or "",
                "hidden": bool(previous.get("hidden")),
            },
            DEFAULT_SERVICE,
        )
    except Exception:
        pass  # the run already failed; the report carries the real cause
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
