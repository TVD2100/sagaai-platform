"""
core.marketplace.catalog - the read model behind the marketplace showcase.

The UI page must not contain business rules: it asks this module for a list
of cards and for one card's details, and only paints the answer. That
split is what lets the showcase be tested without Streamlit and keeps the
"is this installed / can it be installed here" logic in one place.

Two levels of detail are deliberate. The catalog index carries a summary
per entity (name, description, version), which is enough to render the
list; the full manifest - file hashes, permissions, licence - is fetched
only when the user opens one card. A showcase that downloaded every
manifest on every render would spend tens of requests to paint a list.

All four entity types share this read model: ``agent``, ``skill``, ``rag``
and ``mcp``. The registry lookup is per type, because two entities of
different types may legitimately carry the same catalog id.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from core.marketplace import config as marketplace_config
from core.marketplace import registry, source as source_mod

#: Languages the catalog must provide, in display priority order (D-26).
LANGS = ("en", "ru", "zh-CN")

#: Entity types the marketplace knows, in display order.
ENTITY_TYPES = ("agent", "skill", "rag", "mcp")


def localized(value, lang: str = "en") -> str:
    """Return the best text for *lang* from a string or an i18n mapping.

    Falls back to ``en``, then to any non-empty translation, then to an
    empty string. A manifest that ships a bare string is accepted as-is:
    the index summary of an older entity may predate the i18n requirement.
    """
    if isinstance(value, dict):
        for candidate in (lang, "en"):
            text = str(value.get(candidate) or "").strip()
            if text:
                return text
        for text in value.values():
            if str(text).strip():
                return str(text).strip()
        return ""
    return str(value or "").strip()


def _index_entries(source, force: bool) -> dict:
    """Return the catalog index, or a structured failure."""
    try:
        return {"ok": True, "index": source.fetch_index(force=force)}
    except Exception as exc:
        return {
            "ok": False,
            "error_code": getattr(exc, "code", "catalog_unavailable"),
            "message": str(getattr(exc, "message", exc)),
        }


def _installed_map(type_: Optional[str] = None) -> Dict[str, dict]:
    """Return installed entities keyed by catalog id.

    With *type_* the map covers one type; without it every type is read and
    the key carries the type prefix, because a catalog id may be used by
    two entities of different types.
    """
    try:
        rows = registry.list_installed(type_)
    except Exception:
        # A showcase that cannot read the registry still shows the catalog;
        # every card simply looks not installed.
        return {}
    if type_ is not None:
        return {str(row["id"]): row for row in rows}
    return {f"{row.get('type')}:{row.get('id')}": row for row in rows}


def _entry_type(entry: dict) -> str:
    """Return the entity type of an index entry (``skill`` when absent)."""
    return str(entry.get("type") or "skill").strip()


def list_entities(
    type_: Optional[str] = None,
    source: Optional[source_mod.MarketplaceSource] = None,
    lang: str = "en",
    force: bool = False,
) -> dict:
    """Return the cards of one entity type, or of every type.

    Parameters:
        type_: ``agent`` / ``skill`` / ``rag`` / ``mcp``; ``None`` or ``"all"``
            returns every type and tags each item with its own ``type``.
        source: catalog transport; ``None`` uses the configured catalog.
        lang: language of the rendered names and descriptions.
        force: bypass the conditional GET and ask the catalog again.

    Returns a dict with ``ok``, ``items`` (one summary per entity), ``stale``
    (the index came from the cache because the catalog was unreachable),
    ``installed`` (how many cards are already installed) and ``by_type``
    (counts per type, used by the tabs). A failure keeps ``ok=False`` plus
    ``error_code``/``message`` so the page can paint a readable explanation
    instead of an empty list.
    """
    settings = marketplace_config.marketplace_settings()
    transport = source or source_mod.default_source()
    fetched = _index_entries(transport, force)
    if not fetched["ok"]:
        return {
            "ok": False,
            "items": [],
            "stale": False,
            "installed": 0,
            "by_type": {},
            "error_code": fetched["error_code"],
            "message": fetched["message"],
        }

    wanted = str(type_ or "all").strip()
    index = fetched["index"] if isinstance(fetched["index"], dict) else {}
    installed = _installed_map(None if wanted in ("", "all") else wanted)
    items: List[dict] = []
    for entry in index.get("entities") or []:
        if not isinstance(entry, dict):
            continue
        entry_type = _entry_type(entry)
        if wanted not in ("", "all") and entry_type != wanted:
            continue
        entity_id = str(entry.get("id") or "").strip()
        if not entity_id:
            continue
        row = installed.get(entity_id if wanted not in ("", "all") else f"{entry_type}:{entity_id}")
        items.append({
            "id": entity_id,
            "type": entry_type,
            "agent_kind": (
                str(entry.get("agent_kind") or "orchestrator")
                if entry_type == "agent" else ""
            ),
            "name": localized(entry.get("name"), lang) or entity_id,
            "description": localized(entry.get("description"), lang),
            "version": str(entry.get("version") or ""),
            "author": localized(entry.get("author"), lang),
            "updated_at": str(entry.get("updated_at") or ""),
            "installed": bool(row),
            "installed_version": str(row.get("version") or "") if row else "",
            "user_modified": bool(row.get("user_modified")) if row else False,
        })

    items.sort(key=lambda item: (bool(item["installed"]), item["name"].lower()))
    by_type: Dict[str, int] = {}
    for item in items:
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1
    return {
        "ok": True,
        "items": items,
        "stale": bool(index.get("stale")),
        "installed": sum(1 for item in items if item["installed"]),
        "by_type": by_type,
        "channel": settings.get("channel"),
    }


def list_skills(
    source: Optional[source_mod.MarketplaceSource] = None,
    lang: str = "en",
    force: bool = False,
) -> dict:
    """Return the skill cards of the catalog.

    Kept as the skill-only entry point: callers that predate the four-type
    showcase keep working, and the page asks :func:`list_entities` directly.
    """
    return list_entities("skill", source=source, lang=lang, force=force)


def card(entity: dict, lang: str = "en") -> dict:
    """Build the display model of one entity from its manifest.

    The card states what installing it would do before the user presses the
    button: the licence, whether the package carries executable code, which
    tools it declares and which platform it needs. Everything here is read
    from the manifest, nothing is inferred from the installation state.
    """
    entity_id = str(entity.get("id") or "")
    entity_type = _entry_type(entity)
    version = str(entity.get("version") or "")
    licence = entity.get("license") if isinstance(entity.get("license"), dict) else {}
    permissions = entity.get("permissions") if isinstance(entity.get("permissions"), dict) else {}
    platform = entity.get("platform") if isinstance(entity.get("platform"), dict) else {}
    files = entity.get("files") if isinstance(entity.get("files"), dict) else {}
    package = entity.get("package") if isinstance(entity.get("package"), dict) else {}
    distribution = entity.get("distribution") if isinstance(entity.get("distribution"), dict) else {}
    # A base may be served as a small package plus a separate index file, so
    # what the user downloads is both. A base that ships only sources has no
    # index block and the package alone is the answer; a base whose package
    # carries no files reports how many documents it holds instead of zero.
    file_count = len(files) or int(distribution.get("sources_count") or 0)
    size_bytes = int(package.get("size") or 0) + int(distribution.get("index_bytes") or 0)

    row = registry.get_installed(entity_type, entity_id) if entity_id else None
    if row is None:
        state = "not_installed"
    elif str(row.get("version") or "") == version:
        state = "installed"
    else:
        state = "update_available"

    return {
        "id": entity_id,
        "type": entity_type,
        "agent_kind": (
            str(entity.get("agent_kind") or "orchestrator") if entity_type == "agent" else ""
        ),
        "name": localized(entity.get("name"), lang) or entity_id,
        "description": localized(entity.get("description"), lang),
        "version": version,
        "state": state,
        "installed_version": str(row.get("version") or "") if row else "",
        "user_modified": bool(row.get("user_modified")) if row else False,
        "license_spdx": str(licence.get("spdx") or ""),
        "license_name": localized(licence.get("name"), lang),
        "acceptance_required": bool(licence.get("acceptance_required")),
        "executes_code": bool(permissions.get("executes_code")),
        "requires_tools": [str(tool) for tool in (entity.get("requires_tools") or [])],
        "requires_secrets": _required_secrets(entity, permissions),
        "platform_min": str(platform.get("min") or ""),
        "platform_max": str(platform.get("max") or ""),
        "file_count": file_count,
        "size_bytes": size_bytes,
        "skipped_code": list(row.get("skipped_code_files_json") or []) if row else [],
        "recommends": _recommends(entity),
        "bundle": _bundle(entity),
        "distribution": distribution,
    }


def _required_secrets(entity: dict, permissions: dict) -> List[str]:
    """Return the credential field names a card has to ask the user for.

    ``permissions.secrets`` is the generic form. An MCP entity declares its
    fields as ``secret_env_fields`` instead - that is the shape the
    connector layer consumes - so its env names are collected from the
    preset, with the preset-level and config-level declarations treated
    alike. A card that showed no credential requirement for such a server
    would hide the one step the user must take after installing it.
    """
    names = [str(name) for name in (permissions.get("secrets") or [])]
    if _entry_type(entity) != "mcp":
        return names
    preset = entity.get("preset") if isinstance(entity.get("preset"), dict) else {}
    config = preset.get("config") if isinstance(preset.get("config"), dict) else {}
    for declared in (config.get("secret_env_fields"), preset.get("secret_env_fields")):
        if not isinstance(declared, dict):
            continue
        for descriptor in declared.values():
            if not isinstance(descriptor, dict):
                continue
            env = str(descriptor.get("env") or "").strip()
            if env:
                names.append(env)
    return sorted(set(names))


def _recommends(entity: dict) -> List[dict]:
    """Return the recommended entities of an agent manifest as plain dicts.

    The list is normalized here so the page never has to tell a bare id
    from a ``{id, type, default_selected}`` record.
    """
    result: List[dict] = []
    for raw in entity.get("recommends") or []:
        if isinstance(raw, str) and raw.strip():
            result.append({"id": raw.strip(), "type": "", "default_selected": True})
        elif isinstance(raw, dict) and str(raw.get("id") or "").strip():
            result.append({
                "id": str(raw["id"]).strip(),
                "type": str(raw.get("type") or "").strip(),
                "default_selected": bool(raw.get("default_selected", True)),
            })
    return result


def _bundle(entity: dict) -> List[dict]:
    """Return the members of an agent bundle as plain dicts (same shape)."""
    bundle = entity.get("bundle")
    if not isinstance(bundle, (list, tuple)):
        return []
    result: List[dict] = []
    for raw in bundle:
        if isinstance(raw, str) and raw.strip():
            result.append({"id": raw.strip(), "type": "", "default_selected": True})
        elif isinstance(raw, dict) and str(raw.get("id") or "").strip():
            result.append({
                "id": str(raw["id"]).strip(),
                "type": str(raw.get("type") or "").strip(),
                "default_selected": bool(raw.get("default_selected", True)),
            })
    return result


def fetch_card(
    entity_id: str,
    source: Optional[source_mod.MarketplaceSource] = None,
    lang: str = "en",
    version: Optional[str] = None,
    type_: str = "skill",
) -> dict:
    """Fetch one manifest from the catalog and return its card.

    Returns ``{"ok": False, "error_code", "message"}`` when the manifest
    cannot be fetched, so the caller renders the failure inside the card
    instead of losing the whole page.
    """
    transport = source or source_mod.default_source()
    try:
        entity = transport.fetch_entity(str(type_ or "skill"), entity_id, version)
    except Exception as exc:
        return {
            "ok": False,
            "error_code": getattr(exc, "code", "entity_unavailable"),
            "message": str(getattr(exc, "message", exc)),
        }
    view = card(entity, lang)
    view["ok"] = True
    view["entity"] = entity
    return view
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
