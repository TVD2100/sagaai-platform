"""
core.marketplace.migration - adopting entities installed before the catalog.

Entities that shipped inside the platform (a bundled orchestrator, a
prebuilt knowledge base, a preset skill, a connector preset) already exist
on an installation that predates the marketplace. Without adoption the
registry would report them as "not installed" and offer to install a
second copy next to the one the user has been working with.

:data:`LEGACY_DEFAULTS` maps every such entity to the synthetic version that
came with the platform. A synthetic version is deliberately lower than the
first catalog release: an adopted installation then shows a real update
instead of pretending to be current. A version whose files hash to the
catalog tree is re-stamped rather than updated, so a user who never touched
the entity does not download anything (D-22).

The module only reads the catalog card of nothing - it works entirely from
what is on disk - and writes a registry row per adopted entity. Removal
markers left by the pre-marketplace code paths are carried over, so an
entity the user deleted stays deleted.
"""
from __future__ import annotations

import os
from typing import Dict, List, Tuple

from core.marketplace import registry, store


#: Entities the platform used to ship inside ``defaults/``.
#:
#: ``version`` is the synthetic version of the bundled copy, ``detect`` names
#: the detector that decides whether the entity is present, and ``path`` is
#: the runtime location the detector looks at. Keys are ``(type, id)``.
LEGACY_DEFAULTS: Dict[Tuple[str, str], dict] = {
    ("agent", "ya_agent"): {
        "version": "2.0.0",
        "detect": "slug_or_folder",
        "path": "orchestrators/ya_agent",
    },
    ("rag", "yaagentai_2020"): {
        "version": "1.0.0",
        "detect": "manifest_source",
        "path": "rag_bases/yaagentai_2020",
    },
    ("skill", "rag_base_creator"): {
        "version": "1.0.0",
        "detect": "registry_source",
        "path": "skills",
    },
    ("mcp", "github_mcp"): {
        "version": "1.0.0",
        "detect": "config_preset",
        "path": "connectors",
    },
}

#: Marker the pre-marketplace RAG importer wrote into a base manifest.
_DEFAULTS_RAG_SOURCE = "defaults/rag_bases/{slug}"


# ─── Data directory ───────────────────────────────────────────────────────

def _data_dir() -> str:
    """Return the live data directory (``DATA_DIR``), best effort."""
    try:
        from core import paths

        return str(paths.DATA_DIR)
    except Exception:
        return store.data_dir()


# ─── Detectors ────────────────────────────────────────────────────────────

#: Function names of the type the entity is detected on.
#:
#: An agent is adopted when the platform created its folder (the bundled
#: default did that on first boot), a base when its manifest still carries
#: the defaults source marker, a skill when its library record points at
#: ``defaults/``, and an MCP server when a connection was instantiated from
#: the bundled preset. Every detector reports the runtime slug and answers an
#: empty string when the entity is absent.

def _detect_slug_or_folder(spec: dict, entity_id: str, type_: str) -> str:
    """An agent is present when its orchestrator folder exists."""
    if type_ != "agent":
        return ""
    try:
        from core.orchestrator_folders import get_orchestrator_dir

        folder = get_orchestrator_dir(entity_id)
    except Exception:
        return ""
    return entity_id if os.path.isdir(folder) else ""


def _detect_manifest_source(spec: dict, entity_id: str, type_: str) -> str:
    """A base is present when its manifest carries the defaults marker."""
    if type_ != "rag":
        return ""
    try:
        from core import rag

        folder = rag.base_dir(entity_id)
        manifest = rag.json_load(os.path.join(folder, "manifest.json"))
    except Exception:
        return ""
    if not isinstance(manifest, dict):
        return ""
    source = str(manifest.get("source") or "").strip()
    if source == _DEFAULTS_RAG_SOURCE.format(slug=entity_id):
        return entity_id
    return ""


def _detect_registry_source(spec: dict, entity_id: str, type_: str) -> str:
    """A skill is present when its library record points at ``defaults/``."""
    if type_ != "skill":
        return ""
    try:
        from core.skills_library import _load_registry

        records = _load_registry()
    except Exception:
        return ""
    for record in (records or {}).values():
        if not isinstance(record, dict):
            continue
        source = str(record.get("source") or "").strip()
        folder = str(record.get("folder") or "").strip()
        if folder != entity_id or not source.startswith("defaults/"):
            continue
        return entity_id
    return ""


def _detect_config_preset(spec: dict, entity_id: str, type_: str) -> str:
    """An MCP server is present when a connection uses its preset."""
    if type_ != "mcp":
        return ""
    try:
        from core import connectors

        connections = connectors.list_connections()
    except Exception:
        return ""
    for connection in connections or []:
        config = connection.get("config") if isinstance(connection.get("config"), dict) else {}
        if str(config.get("preset") or "").strip() == entity_id:
            return str(connection.get("id") or "") or entity_id
    return ""


_DETECTORS = {
    "slug_or_folder": _detect_slug_or_folder,
    "manifest_source": _detect_manifest_source,
    "registry_source": _detect_registry_source,
    "config_preset": _detect_config_preset,
}


def detect(spec: dict, entity_id: str, type_: str) -> str:
    """Return the runtime slug of a bundled entity, or an empty string.

    An unknown detector name answers an empty string instead of raising: a
    registry written by a newer platform must not break the start-up of an
    older one.
    """
    detector = _DETECTORS.get(str(spec.get("detect") or ""))
    if detector is None:
        return ""
    try:
        return detector(spec, entity_id, type_) or ""
    except Exception:
        return ""


# ─── Hashing area ─────────────────────────────────────────────────────────

def _area_files(type_: str, runtime_slug: str) -> Dict[str, str]:
    """Return the hashing area of an adopted entity.

    The area is what the installer would have recorded: the agent part of an
    orchestrator folder, the declared files of a skill, the non-volatile
    files of a base. A connector is a record rather than a folder, so it
    carries no files at all.
    """
    if type_ == "mcp":
        return {}
    if type_ == "agent":
        from core.marketplace.agent_installer import area_files

        return area_files(runtime_slug)
    return registry.entity_file_manifest(type_, runtime_slug)


# ─── Removal markers ──────────────────────────────────────────────────────

def carry_removal_markers() -> List[dict]:
    """Copy pre-marketplace delete markers into ``marketplace_removed``.

    The RAG importer recorded a deletion in ``rag_bases/.defaults_removed.json``
    and the skills library in its own removed-defaults file. Those markers
    live outside the registry, so a migration that only looks at the
    catalog would resurrect exactly the entities the user deleted.

    Returns the markers that were written.
    """
    carried: List[dict] = []
    for type_, entity_id, sources in _legacy_removal_candidates():
        if not any(source in sources for source in _legacy_markers_for(type_, entity_id)):
            continue
        if registry.is_removed(type_, entity_id):
            continue
        try:
            registry.mark_removed(type_, entity_id, entity_id, via="legacy")
        except Exception:
            continue
        carried.append({"type": type_, "id": entity_id})
    return carried


def _legacy_markers_for(type_: str, entity_id: str) -> List[str]:
    """Return the pre-marketplace marker strings that mean "deleted"."""
    if type_ == "rag":
        return [_DEFAULTS_RAG_SOURCE.format(slug=entity_id)]
    if type_ == "skill":
        return [f"defaults/{entity_id}"]
    return []


def _legacy_markers() -> Dict[str, List[str]]:
    """Return ``{marker type: markers}`` read from the pre-marketplace files."""
    markers: Dict[str, List[str]] = {"rag": [], "skill": []}
    try:
        from core import rag

        markers["rag"] = [str(item) for item in rag._load_removed_defaults()]
    except Exception:
        pass
    try:
        from core.skills_library import _load_removed_defaults

        markers["skill"] = [str(item) for item in _load_removed_defaults()]
    except Exception:
        pass
    return markers


def _legacy_removal_candidates() -> List[Tuple[str, str, List[str]]]:
    """Return ``(type, id, markers of that type)`` for every mapped entity."""
    markers = _legacy_markers()
    return [
        (type_, entity_id, markers.get(type_, []))
        for (type_, entity_id) in LEGACY_DEFAULTS
    ]


# ─── Seeding ──────────────────────────────────────────────────────────────

def _record(
    type_: str,
    entity_id: str,
    runtime_slug: str,
    version: str,
    files: Dict[str, str],
) -> dict:
    """Build the registry row of an adopted entity."""
    now = store.isoformat_utc()
    runtime_db_id = None
    runtime_skill_id = None
    agent_kind = None
    if type_ == "agent":
        # An agent is either an employee or a helper, and the two live in
        # different tables; the folder that exists decides which row the
        # record points at.
        from core.marketplace.agent_installer import area_files  # noqa: F401

        try:
            orchestrator_dir = os.path.join(registry.agents_root(), runtime_slug)
            agent_kind = "orchestrator" if os.path.isdir(orchestrator_dir) else "assistant"
        except Exception:
            agent_kind = "orchestrator"
        try:
            if agent_kind == "assistant":
                from core.assistants import get_assistant_by_slug

                row = get_assistant_by_slug(runtime_slug) or {}
            else:
                from core.orchestrators import get_orchestrator

                row = get_orchestrator(runtime_slug) or {}
            runtime_db_id = str(row.get("id") or "") or None
        except Exception:
            runtime_db_id = None
    if type_ == "skill":
        try:
            from core.skills_library import list_skills

            for skill in list_skills():
                if str(skill.get("folder") or "") == runtime_slug:
                    runtime_skill_id = str(skill.get("id") or "") or None
                    break
        except Exception:
            runtime_skill_id = None
    record = {
        "type": type_,
        "id": entity_id,
        "runtime_slug": runtime_slug,
        "runtime_db_id": runtime_db_id,
        "runtime_skill_id": runtime_skill_id,
        "fork_of": None,
        "version": version,
        "channel": "legacy",
        "source_url": "legacy",
        "package_sha256": None,
        "tree_sha256": registry.manifest_sha256(files),
        "index_sha256": None,
        "files_json": files,
        "skipped_code_files_json": [],
        "installed_at": now,
        "updated_at": now,
        "installed_by": "legacy_defaults",
        "user_modified": False,
        "user_modified_files_json": [],
        "install_id": None,
        "bundle_id": None,
        "pinned": False,
        "license_accepted_at": None,
    }
    if agent_kind:
        record["agent_kind"] = agent_kind
    return record


def seed_legacy_entities() -> List[dict]:
    """Register every bundled entity that is present but not in the registry.

    An entity that is already registered is left untouched, and one the user
    deleted (a removal marker exists, in the registry or in the markers the
    pre-marketplace code wrote) is skipped - adoption must never resurrect
    something the user got rid of.

    Returns the rows that were written.
    """
    installed = {(row["type"], row["id"]) for row in registry.list_installed()}
    seeded: List[dict] = []
    for (type_, entity_id), spec in LEGACY_DEFAULTS.items():
        if (type_, entity_id) in installed:
            continue
        if registry.is_removed(type_, entity_id):
            continue
        runtime_slug = detect(spec, entity_id, type_)
        if not runtime_slug:
            continue
        files = _area_files(type_, runtime_slug)
        record = _record(
            type_, entity_id, runtime_slug, str(spec.get("version") or "0.0.0"), files
        )
        try:
            registry.upsert_installed(record)
        except Exception:
            continue
        seeded.append(record)
    if seeded:
        registry.export_registry_cache()
    return seeded


def migrate() -> dict:
    """Adopt bundled entities and carry their removal markers over.

    The two halves are independent on purpose: carrying a marker is what
    keeps a deleted entity deleted, and adopting one is what keeps an
    installed entity installed. A failure in either half is reported in the
    result instead of escaping into the start-up path.
    """
    result: Dict[str, object] = {"ok": True, "seeded": [], "carried": [], "error": ""}
    try:
        result["carried"] = carry_removal_markers()
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"removal markers: {exc}"
    try:
        result["seeded"] = seed_legacy_entities()
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
    return result


def is_legacy_entity(type_: str, entity_id: str) -> bool:
    """True when ``(type, id)`` is one of the entities the platform shipped."""
    return (str(type_ or ""), str(entity_id or "")) in LEGACY_DEFAULTS


def legacy_ids_of(type_: str) -> List[str]:
    """Return the catalog ids of the bundled entities of one type."""
    return sorted(
        entity_id
        for (declared, entity_id) in LEGACY_DEFAULTS
        if declared == str(type_ or "")
    )
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
