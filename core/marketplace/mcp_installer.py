"""
core.marketplace.mcp_installer - installation of an ``mcp`` entity.

An MCP entity ships no files at all: it is a preset of a connector - the
transport config, the description of the credential fields and a memory
seed. Installing it means creating a **connection** from that preset, so the
installer here is a thin, careful layer over
:mod:`core.connector_presets` and :mod:`core.connectors`.

Two properties decide the shape of this module:

  * identity is ``(service, preset_id)``, not a folder name (D-09). The
    connection id is a random uuid, so the same preset installed twice would
    otherwise produce two connections that look unrelated. Every install
    therefore looks for an existing connection whose ``config.preset``
    matches, and adopts it instead of creating a duplicate.
  * the catalog can never carry a credential. A preset declares
    ``secret_env_fields`` - names and labels of the fields the user fills in
    - and the installer refuses a preset that carries a value for any of
    them. The connection is created as a draft, so the tile appears in the
    connectors page waiting for the user's credentials.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from core.marketplace import config as marketplace_config
from core.marketplace import registry, source as source_mod, store
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

#: Charset a preset id may use (mirrors ``core.connector_presets``).
_PRESET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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

    The id doubles as the registry ``runtime_slug``: it is what the
    deduplication compares and what the connectors page shows in the
    connection's ``config.preset``.
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

    The config is what ``create_connection``/``update_connection`` receive:
    the preset transport fields, the credential field descriptors and the
    preset id itself, which is what makes the connection deduplicable.
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
            f"Preset id '{preset_id or '-'}' is not usable: [A-Za-z0-9_-], up to 64 chars",
            {"id": str(entity.get("id") or "")},
        )
    merged["preset"] = preset_id
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


# ─── Connection lookup ────────────────────────────────────────────────────

def find_connection(service: str, preset_id: str) -> Optional[dict]:
    """Return the existing connection created from *preset_id*, or None.

    ``config.preset`` is the only link between a catalog entity and a
    connection: the connection id is random, so matching by folder name
    cannot work (D-09). Several connections from one preset are possible -
    a user may keep two accounts of the same server - and the first one is
    reported so an install does not create a third.
    """
    if not preset_id:
        return None
    try:
        from core import connectors

        manifests = connectors.list_connections()
    except Exception:
        return None
    for manifest in manifests:
        if str(manifest.get("service") or "") != service:
            continue
        config = manifest.get("config") if isinstance(manifest.get("config"), dict) else {}
        if str(config.get("preset") or "") == preset_id:
            return manifest
    return None


def _connection_config(manifest: dict) -> Dict[str, Any]:
    """Return the comparable part of a connection config."""
    config = manifest.get("config") if isinstance(manifest.get("config"), dict) else {}
    return {str(key): value for key, value in config.items()}


# ─── Install ──────────────────────────────────────────────────────────────

def install_mcp(
    entity: dict,
    *,
    channel: str,
    allow_code: Optional[bool] = None,
    source: Optional[source_mod.MarketplaceSource] = None,
    accept_license: bool = False,
    runtime_slug: Optional[str] = None,
    adopt_existing: bool = True,
) -> dict:
    """Install one MCP server from the catalog and return a report.

    Parameters:
        entity: parsed ``entity.json`` of the server.
        channel: release channel recorded on the registry row.
        allow_code: unused (an MCP entity ships no files); accepted so every
            installer shares one call signature.
        source: catalog transport; unused, kept for the same reason.
        accept_license: explicit licence acceptance when required.
        runtime_slug: connection id to reuse; ``None`` picks the existing
            connection of the same preset or creates a new one.
        adopt_existing: when a connection from this preset already exists
            but has no registry row, register it instead of creating a
            second one. Passing ``False`` forces a new connection.

    Returns a report with ``connection_id``, ``preset`` and ``service``. A
    re-install of the same version is a no-op unless the preset declares
    non-secret settings that differ from the stored connection - those are
    applied in place, which is what an update of an MCP entity means.
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
    # The revision check comes first: a server the platform cannot talk to is
    # refused for that reason, not for a field of a preset it will never use.
    check_protocol(entity)
    config = preset_config(entity)

    compatible, reason = _platform_compatible(entity)
    if not compatible:
        raise InstallError(
            "platform_incompatible",
            reason or "Entity is not compatible with this platform build",
            {"id": entity_id, "version": version},
        )
    licence_at = _licence_state(entity, accept_license)

    existing_row = registry.get_installed("mcp", entity_id)
    # ``adopt_existing=False`` asks for another instance of the same server -
    # a second account, a second endpoint. Neither the registry row nor the
    # connection found by preset is reused then; the row is repointed at the
    # connection this run creates.
    target_id = str(runtime_slug or "")
    if not target_id and adopt_existing:
        target_id = str((existing_row or {}).get("runtime_slug") or "")
    found = None
    if not target_id:
        found = find_connection(service, preset_id)
        if found is not None and adopt_existing:
            target_id = str(found.get("id") or "")

    from core import connectors

    created = False
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
        try:
            if target_id:
                manifest = connectors.get_connection(target_id) or {}
                if not manifest:
                    raise InstallError(
                        "connection_missing",
                        "The connection this entity refers to no longer exists",
                        {"id": entity_id, "connection_id": target_id},
                    )
                updated = _apply_non_secret_changes(
                    target_id, manifest, config, _display_name(entity)
                )
                adopted = found is not None and not existing_row
            else:
                name = _display_name(entity) or str(preset_of(entity).get("name") or preset_id)
                manifest = connectors.create_connection(
                    service,
                    name,
                    config=config,
                    secrets=None,
                    pending=True,
                )
                created = True
                adopted = False
                updated = False
                target_id = str(manifest.get("id") or "")
                if not target_id:
                    raise InstallError(
                        "connection_not_created",
                        "The connector layer returned no connection id",
                        {"id": entity_id},
                    )
                memory_seed = str(preset_of(entity).get("memory_seed") or "")
                if memory_seed:
                    try:
                        connectors.set_connection_memory(target_id, memory_seed)
                    except Exception:
                        pass

            # A re-install of what is already there touches nothing: the
            # showcase renders such an entity as skipped, and a run that
            # changed no setting must not look like a fresh install.
            unchanged = (
                existing_row is not None
                and not adopted
                and not created
                and not updated
                and str(existing_row.get("version") or "") == version
            )
            if unchanged:
                return {
                    "ok": True,
                    "noop": True,
                    "id": entity_id,
                    "type": "mcp",
                    "version": version,
                    "install_id": existing_row.get("install_id"),
                    "runtime_slug": target_id,
                    "connection_id": target_id,
                    "preset": preset_id,
                    "service": service,
                    "adopted": False,
                    "created": False,
                    "credentials_status": str(manifest.get("credentials_status") or "pending"),
                    "requires_secrets": _secret_field_names(config),
                }

            now = store.isoformat_utc()
            registry.upsert_installed(
                {
                    "type": "mcp",
                    "id": entity_id,
                    "runtime_slug": target_id,
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
            store.update_run(
                install_id,
                status="done",
                finished_at=store.isoformat_utc(),
                runtime_slug=target_id,
            )
            store.clear_state()
            try:
                registry.unmark_removed("mcp", entity_id)
            except Exception:
                pass
            registry.export_registry_cache()
            return {
                "ok": True,
                "noop": False,
                "id": entity_id,
                "type": "mcp",
                "version": version,
                "install_id": install_id,
                "runtime_slug": target_id,
                "connection_id": target_id,
                "preset": preset_id,
                "service": service,
                "adopted": bool(adopted),
                "created": bool(created),
                "credentials_status": str(manifest.get("credentials_status") or "pending"),
                "requires_secrets": _secret_field_names(config),
            }
        except Exception as exc:
            if created and target_id:
                try:
                    connectors.delete_connection(target_id, record_removal=False)
                except Exception:
                    pass
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


def _secret_field_names(config: dict) -> List[str]:
    """Return the env names a user still has to fill in."""
    declared = config.get("secret_env_fields")
    if not isinstance(declared, dict):
        return []
    names = []
    for descriptor in declared.values():
        if isinstance(descriptor, dict) and descriptor.get("env"):
            names.append(str(descriptor["env"]))
    return sorted(names)


def _apply_non_secret_changes(
    conn_id: str,
    manifest: dict,
    config: Dict[str, Any],
    name: str,
) -> bool:
    """Bring an existing connection up to the catalog's non-secret settings.

    Only what the connection already stores is compared: a key the catalog
    dropped is not removed, because the user may have added it deliberately
    and the connector layer merges rather than replaces. Credentials are
    never touched - the catalog carries none.

    Returns True when something was written, so the caller can tell a real
    change from a re-install of what is already there.
    """
    if not conn_id:
        return False
    from core import connectors

    stored = _connection_config(manifest)
    changed = {
        key: value for key, value in config.items()
        if key != "description" and stored.get(key) != value
    }
    rename = str(name or "").strip()
    name_now = str(manifest.get("name") or "").strip()
    if not changed and (not rename or rename == name_now):
        return False
    try:
        connectors.update_connection(
            conn_id,
            name=rename if rename and rename != name_now else "",
            config=changed or None,
        )
    except ValueError as exc:
        raise InstallError(
            "connection_update_failed",
            f"The connection could not be updated: {exc}",
            {"connection_id": conn_id},
        ) from exc
    return True
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
