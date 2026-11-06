"""
core.marketplace.agent_preflight - validating an agent install request.

Everything that can refuse an agent install happens here, before the first
download: the marketplace switch, the declared file manifest, the agent kind,
the platform bounds, the licence and the executable-code policy. The two
installers then differ only in where the folder lives and which importer turns
the package into a database row.

This module also owns the folder-name rules: an update keeps the folder the
entity already occupies, a new entity gets a free name with a suffix on
collision.
"""
from __future__ import annotations

from typing import List, Optional

from core.marketplace import config as marketplace_config
from core.marketplace import registry
from core.marketplace import source as source_mod
from core.marketplace.installer import (
    InstallError,
    _entity_files,
    _licence_state,
    _platform_compatible,
    _split_code_files,
)

#: Agent kinds the installer can place on disk.
#:
#: An ``orchestrator`` is an employee with instructions and custom
#: functions; an ``assistant`` is a one-shot profile whose folder holds a
#: manifest, a prompt and optional attachment files. Both are agents in the
#: catalog and differ only in where the folder lives and which importer
#: turns it into a database row.
SUPPORTED_KINDS = ("orchestrator", "assistant")


def _slug_taken(slug: str, agent_kind: str = "orchestrator") -> bool:
    """True when a folder or DB row of *agent_kind* already uses *slug*."""
    if str(agent_kind or "") == "assistant":
        from core.assistant_folders import assistant_folder_exists

        if assistant_folder_exists(slug):
            return True
        try:
            from core.assistants import get_assistant_by_slug

            return get_assistant_by_slug(slug) is not None
        except Exception:
            return False
    from core.orchestrator_folders import orchestrator_folder_exists

    if orchestrator_folder_exists(slug):
        return True
    try:
        from core.orchestrators import get_orchestrator

        return get_orchestrator(slug) is not None
    except Exception:
        return False


def _pick_runtime_slug(
    entity: dict,
    requested: Optional[str] = None,
    agent_kind: str = "orchestrator",
) -> str:
    """Return a free folder name for the agent.

    The installer assigns a suffix on collision (D-06) instead of leaving it
    to the importer: the orchestrator importer renames silently, which would
    make the registry row point at a folder nobody expects, and the assistant
    importer suffixes only through its own naming rule.
    """
    raw = str(requested or entity.get("slug") or entity.get("id") or "agent")
    if str(agent_kind or "") == "assistant":
        from core.assistant_folders import normalize_slug

        base = normalize_slug(raw) or "assistant"
    else:
        from core.orchestrator_folders import safe_orchestrator_slug

        base = safe_orchestrator_slug(raw) or "agent"
    candidate = base
    counter = 2
    while _slug_taken(candidate, agent_kind):
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def _resolve_slug(
    entity: dict,
    existing: Optional[dict],
    requested: Optional[str] = None,
    agent_kind: str = "orchestrator",
) -> str:
    """Return the folder name this install must use.

    An update keeps the folder the entity already occupies: the registry row
    names it, and picking a fresh free name instead would leave a second copy
    of the same agent next to the first one. A new entity gets a free name,
    with a suffix when the folder belongs to something else (D-06).
    """
    if requested:
        if str(agent_kind or "") == "assistant":
            from core.assistant_folders import normalize_slug

            return normalize_slug(requested) or requested
        from core.orchestrator_folders import safe_orchestrator_slug

        return safe_orchestrator_slug(requested) or requested
    installed_slug = str((existing or {}).get("runtime_slug") or "")
    if installed_slug:
        return installed_slug
    return _pick_runtime_slug(entity, agent_kind=agent_kind)


def _agent_kind(entity: dict) -> str:
    """Return the validated kind of an agent manifest.

    A manifest without ``agent_kind`` is an orchestrator: that is the only
    kind the catalog carried before helpers became installable.
    """
    kind = str((entity or {}).get("agent_kind") or "orchestrator").strip()
    if kind not in SUPPORTED_KINDS:
        raise InstallError(
            "unsupported_agent_kind",
            f"Agent kind '{kind}' cannot be installed by the marketplace",
            {
                "id": str((entity or {}).get("id") or ""),
                "agent_kind": kind,
                "supported": list(SUPPORTED_KINDS),
            },
        )
    return kind


def _install_preflight(
    entity: dict,
    *,
    allow_code: Optional[bool],
    source: Optional[source_mod.MarketplaceSource],
    accept_license: bool,
) -> dict:
    """Validate an agent install request and return the shared values.

    Everything that can refuse the run happens here, before the first
    download: the marketplace switch, the declared file manifest, the
    platform bounds, the licence and the executable-code policy. The two
    installers then differ only in where the folder lives and which importer
    turns it into a database row.

    The "same version already installed" shortcut answers ``{"noop": ...}``
    and nothing else, so a caller returns it before looking at the rest.
    """
    settings = marketplace_config.marketplace_settings()
    if not settings.get("enabled"):
        raise InstallError(
            "marketplace_disabled",
            "The marketplace is disabled in the settings",
        )

    entity_id = str(entity["id"])
    version = str(entity.get("version") or "0.0.0")
    declared_all = _entity_files(entity)
    if not declared_all:
        raise InstallError(
            "missing_file_manifest",
            "Entity manifest declares no files",
            {"id": entity_id},
        )

    existing = registry.get_installed("agent", entity_id)
    if existing is not None:
        wanted = str(entity.get("agent_kind") or "orchestrator").strip()
        recorded = str(existing.get("agent_kind") or "orchestrator").strip()
        if wanted != recorded:
            # The kind decides the folder and the database table, so an
            # installation cannot change it in place: the old one has to be
            # removed first, explicitly.
            raise InstallError(
                "agent_kind_changed",
                f"The installed agent is a {recorded}, the catalog ships a {wanted}",
                {"id": entity_id, "installed": recorded, "catalog": wanted},
            )
    if existing is not None and str(existing.get("version")) == version:
        return {
            "noop": {
                "ok": True,
                "noop": True,
                "id": entity_id,
                "type": "agent",
                "agent_kind": str(existing.get("agent_kind") or "orchestrator"),
                "runtime_slug": existing.get("runtime_slug"),
                "version": version,
                "install_id": existing.get("install_id"),
                "skipped_code_files": list(
                    existing.get("skipped_code_files_json") or []
                ),
            }
        }

    compatible, reason = _platform_compatible(entity)
    if not compatible:
        raise InstallError(
            "platform_incompatible",
            reason or "Entity is not compatible with this platform build",
            {"id": entity_id, "version": version},
        )

    license_at = _licence_state(entity, accept_license)
    install_code = settings["allow_code"] if allow_code is None else bool(allow_code)
    non_code, code_files = _split_code_files(declared_all)
    if install_code:
        declared = dict(declared_all)
        skipped_code: List[str] = []
    else:
        declared = dict(non_code)
        skipped_code = list(code_files)
        if not declared:
            raise InstallError(
                "nothing_to_install",
                "Every file of the agent is executable and allow_code is off",
                {"id": entity_id, "skipped": skipped_code},
            )

    return {
        "entity_id": entity_id,
        "version": version,
        "existing": existing,
        "declared": declared,
        "skipped_code": skipped_code,
        "install_code": install_code,
        "license_at": license_at,
        "catalog": source or source_mod.default_source(),
    }


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
