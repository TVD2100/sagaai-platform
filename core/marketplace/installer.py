"""
core.marketplace.installer - skill installation with a full rollback path.

A run follows a fixed order (D-07):

  1. platform compatibility check
  2. runtime folder selection
  3. package download and sha256 verification
  4. archive validation and unpacking into staging
  5. per-file verification against the manifest
  6. backup metadata (and a copy of ``skills.json``) written BEFORE the
     first change to the installation
  7. import through the existing skills-library importer
  8. registry-file metadata (``skills.json``) written atomically
  9. ``marketplace_installed`` row written LAST, inside its own session
 10. staging cleanup and the run log entry

Any failure between 4 and 9 triggers the same rollback the user-facing
undo uses: files restored from the backup, the created folder removed, the
``skills.json`` copy restored byte-for-byte and no registry row left
behind.

The installer never talks to the network directly - it asks a
``MarketplaceSource`` for the package, and tests inject their own source.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Dict, List, Optional, Tuple

from core.marketplace import config as marketplace_config
from core.marketplace import registry, rollback, source as source_mod, store, verify


class InstallError(store.MarketplaceError):
    """Raised when a skill cannot be installed."""


#: Entity types :func:`install_entity` knows how to install.
#:
#: The showcase and the bundle planner read this tuple instead of probing
#: the dispatcher with a trial call: a type missing here is shown as
#: "cannot be installed yet" before the user presses anything.
SUPPORTED_TYPES = ("skill", "agent", "rag", "mcp")


def _current_version() -> str:
    """Return the running platform version as a version string."""
    try:
        from core.version import __version__

        return str(__version__)
    except Exception:
        return "0.0.0"


def _platform_compatible(entity: dict) -> Tuple[bool, str]:
    """Check ``platform.min`` / ``platform.max`` against the running build.

    Returns ``(ok, reason)``; a missing declaration means "any platform".
    An unparsable bound fails closed with an explicit reason rather than
    silently accepting the entity.
    """
    from core.marketplace import semver

    platform = entity.get("platform") if isinstance(entity.get("platform"), dict) else {}
    minimum = str(platform.get("min") or "").strip()
    maximum = str(platform.get("max") or "").strip()
    current = _current_version()
    if not minimum and not maximum:
        return True, ""
    try:
        if minimum and semver.compare(current, minimum) < 0:
            return False, f"requires platform >= {minimum}"
        if maximum and semver.compare(current, maximum) > 0:
            return False, f"requires platform <= {maximum}"
    except semver.SemverError:
        return False, "invalid platform bounds in the manifest"
    return True, ""


def _pick_runtime_slug(entity: dict) -> str:
    """Return the folder name a skill will occupy.

    A free name is used as-is; a taken one gets a numeric suffix through the
    library's own ``_unique_folder`` so the two never disagree about the
    naming rule (``ya_agent_2020`` becomes ``ya_agent_2020_2``).
    """
    from core.skills_library import _unique_folder

    slug = str(entity.get("runtime_slug") or entity.get("id") or "skill").strip()
    slug = slug.replace("/", "_").replace("#", "_") or "skill"
    return _unique_folder(slug)


def _entity_files(entity: dict) -> Dict[str, str]:
    """Return the ``{path: sha256}`` manifest declared by an entity."""
    files = entity.get("files")
    if isinstance(files, dict):
        return {str(k): str(v) for k, v in files.items()}
    package = entity.get("package") if isinstance(entity.get("package"), dict) else {}
    declared = package.get("files")
    if isinstance(declared, dict):
        return {str(k): str(v) for k, v in declared.items()}
    return {}


def _split_code_files(files: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """Split a manifest into installed files and skipped code files."""
    installed: Dict[str, str] = {}
    skipped: List[str] = []
    for rel_path, digest in files.items():
        if verify.is_code_path(rel_path):
            skipped.append(rel_path)
        else:
            installed[rel_path] = digest
    return installed, sorted(skipped)


def _skills_registry_path() -> str:
    """Return the ``skills.json`` path of the skills library."""
    from core.skills_library import _registry_path

    return _registry_path()


def _backup_registry_file(install_id: str) -> str:
    """Copy ``skills.json`` into the run backup and return the relative name.

    The copy is what makes a rollback byte-exact: rewriting the file from
    parsed data would reorder keys and lose records the run never touched.
    """
    path = _skills_registry_path()
    rel_name = "skills.json"
    if not os.path.isfile(path):
        return ""
    target = os.path.join(store.backup_dir(install_id), rel_name)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(path, target)
    return rel_name


def _write_skill_metadata(skill_id: str, **fields) -> None:
    """Merge *fields* into one ``skills.json`` record atomically.

    The shared ``_save_registry`` writes in place; this function insists on
    temp-file + rename so a crash mid-write can never leave the registry
    corrupt (a corrupt registry reads back as an empty one).
    """
    from core.skills_library import _load_registry

    registry_data = _load_registry()
    record = registry_data.get(skill_id)
    if record is None:
        return
    record.update({key: value for key, value in fields.items() if value is not None})
    registry_data[skill_id] = record
    store.atomic_write_json(_skills_registry_path(), registry_data)


def _licence_state(entity: dict, accept_license: bool) -> Optional[str]:
    """Validate the licence gate and return the acceptance timestamp.

    An entity that demands acceptance is refused unless the caller passed
    ``accept_license=True``; acceptance is recorded on the registry row so
    the decision survives a restart instead of living in widget state.
    """
    licence = entity.get("license") if isinstance(entity.get("license"), dict) else {}
    required = bool(licence.get("acceptance_required"))
    if required and not accept_license:
        raise InstallError(
            "license_not_accepted",
            "The entity requires accepting its licence before installation",
            {"spdx": licence.get("spdx"), "id": entity.get("id")},
        )
    return store.isoformat_utc() if required else None


def _utc_now() -> str:
    """Return the current time in the registry's timestamp format."""
    return store.isoformat_utc()


def install_skill(
    entity: dict,
    *,
    channel: str,
    allow_code: Optional[bool] = None,
    source: Optional[source_mod.MarketplaceSource] = None,
    accept_license: bool = False,
) -> dict:
    """Install one skill from the catalog and return a report.

    Parameters:
        entity: parsed ``entity.json`` of the skill.
        channel: release channel name recorded on the registry row.
        allow_code: install ``scripts/*`` files; ``None`` reads the setting.
            The default is ``true`` - the catalog holds the owner's own
            entities (D-12).
        source: catalog transport; tests inject their own, production
            callers leave it ``None`` to use the configured catalog.
        accept_license: explicit licence acceptance for entities that
            require it.

    Re-installing the same version is a no-op: the existing row keeps its
    ``installed_at``, no second folder appears and the run log does not
    grow. Any other failure rolls the installation back and re-raises.
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
    files = _entity_files(entity)
    if not files:
        raise InstallError(
            "missing_file_manifest",
            "Entity manifest declares no files",
            {"id": entity_id},
        )

    existing = registry.get_installed("skill", entity_id)
    if existing is not None and str(existing.get("version")) == version:
        return {
            "ok": True,
            "noop": True,
            "id": entity_id,
            "runtime_slug": existing.get("runtime_slug"),
            "version": version,
            "skill_id": existing.get("runtime_skill_id"),
            "install_id": existing.get("install_id"),
            "skipped_code_files": list(existing.get("skipped_code_files_json") or []),
        }

    compatible, reason = _platform_compatible(entity)
    if not compatible:
        raise InstallError(
            "platform_incompatible",
            reason or "Entity is not compatible with this platform build",
            {"id": entity_id, "version": version, "platform": _current_version()},
        )

    language_accept = _licence_state(entity, accept_license)
    install_code = settings["allow_code"] if allow_code is None else bool(allow_code)
    non_code, code_files = _split_code_files(files)
    if install_code:
        installed_files = dict(files)
        skipped_code: List[str] = []
    else:
        installed_files = non_code
        skipped_code = code_files
        if not installed_files:
            raise InstallError(
                "nothing_to_install",
                "Every file of the entity is executable and allow_code is off",
                {"id": entity_id, "skipped": skipped_code},
            )

    catalog = source or source_mod.default_source()

    with store.install_lock():
        install_id = store.new_install_id()
        staging, _backup = store.prepare_run_dirs(install_id)
        runtime_slug = ""
        created_skill_id = ""
        backup_registry = ""
        version = str(entity.get("version") or "0.0.0")
        # The run is recorded BEFORE the first network call: an attempt that
        # dies during download must still appear in the history, otherwise a
        # failing catalog looks like nothing ever happened.
        store.append_run(
            {
                "install_id": install_id,
                "type": "skill",
                "id": entity_id,
                "version": version,
                "action": "install",
                "status": "applying",
            }
        )
        try:
            package = catalog.fetch_package(entity)
            package_sha = verify.verify_package(
                package, str((entity.get("package") or {}).get("sha256") or "")
            )
            package_path = os.path.join(staging, "package.tar.gz")
            with open(package_path, "wb") as handle:
                handle.write(package)
            # Keep the package for offline operations: the conflict paths use
            # it as the pristine reference for a reset (D-08).
            try:
                store.cache_package(package_sha, package)
            except OSError:
                pass

            members = verify.validate_archive(package_path)
            skip_paths = [name for name in skipped_code] if not install_code else []
            unpack_dir = os.path.join(staging, "content")
            verify.extract_archive(package_path, unpack_dir, members, skip=skip_paths)

            mismatched = verify.verify_files(unpack_dir, installed_files)
            if mismatched:
                raise InstallError(
                    "file_hash_mismatch",
                    "Unpacked files do not match the manifest",
                    {"files": mismatched[:10], "count": len(mismatched)},
                )

            runtime_slug = _pick_runtime_slug(entity)
            # Whether ``skills.json`` existed before the run decides how a
            # rollback restores it: an existing file is copied back from the
            # backup, a missing one is deleted again.
            registry_existed = os.path.isfile(_skills_registry_path())
            backup_registry = _backup_registry_file(install_id)
            previous_row = existing
            store.write_run_meta(
                install_id,
                {
                    "install_id": install_id,
                    "type": "skill",
                    "id": entity_id,
                    "runtime_slug": runtime_slug,
                    "created_folder": True,
                    "created_files": sorted(installed_files),
                    "replaced_files": [],
                    "removed_files": [],
                    "previous_row": previous_row,
                    "registry_path": _skills_registry_path(),
                    "registry_backup": backup_registry,
                    "registry_existed": registry_existed,
                },
            )

            from core.skills_library import import_skill_from_folder

            name = _display_name(entity)
            imported = import_skill_from_folder(
                unpack_dir,
                name=name,
                description=_display_description(entity),
                developer="SagaAI",
                adapted=True,
                folder=runtime_slug,
                skill_id=None,
            )
            created_skill_id = str(imported.get("skill", {}).get("id") or "")
            if not created_skill_id:
                raise InstallError("import_failed", "The skills library returned no id")

            manifest = registry.entity_file_manifest("skill", runtime_slug, installed_files)
            tree = registry.tree_sha256("skill", runtime_slug, installed_files)
            now = store.isoformat_utc()
            _write_skill_metadata(
                created_skill_id,
                version=version,
                updated_at=now,
                installed_at=now,
                source=f"marketplace/{entity_id}",
                sha256=package_sha,
                source_url=str(entity.get("source_url") or ""),
            )

            registry.upsert_installed(
                {
                    "type": "skill",
                    "id": entity_id,
                    "runtime_slug": runtime_slug,
                    "runtime_db_id": None,
                    "runtime_skill_id": created_skill_id,
                    "fork_of": entity.get("fork_of"),
                    "version": version,
                    "channel": channel,
                    "source_url": str(entity.get("source_url") or ""),
                    "package_sha256": package_sha,
                    "tree_sha256": tree,
                    "index_sha256": None,
                    "files_json": manifest,
                    "skipped_code_files_json": skipped_code,
                    "installed_at": now,
                    "updated_at": now,
                    "installed_by": "marketplace",
                    "user_modified": False,
                    "user_modified_files_json": [],
                    "install_id": install_id,
                    "bundle_id": entity.get("bundle_id"),
                    "pinned": False,
                    "license_accepted_at": language_accept,
                }
            )

            store.update_run(
                install_id,
                status="done",
                finished_at=store.isoformat_utc(),
                skill_id=created_skill_id,
                runtime_slug=runtime_slug,
            )
            store.cleanup_run_dirs(install_id, keep_backup=True)
            store.clear_state()
            # An explicit install cancels an earlier removal: without this the
            # marker would keep suppressing the entity in default imports and
            # in the catalog card after the user asked for it back.
            try:
                registry.unmark_removed("skill", entity_id)
            except Exception:
                pass
            registry.export_registry_cache()

            return {
                "ok": True,
                "noop": False,
                "id": entity_id,
                "version": version,
                "install_id": install_id,
                "skill_id": created_skill_id,
                "runtime_slug": runtime_slug,
                "tree_sha256": tree,
                "skipped_code_files": skipped_code,
            }
        except Exception as exc:
            _fail_run(install_id, runtime_slug, created_skill_id, backup_registry, exc)
            if isinstance(exc, InstallError):
                raise
            # A check that failed inside the run keeps its machine-readable
            # code, but callers of ``install_skill`` see one error type.
            if isinstance(exc, store.MarketplaceError):
                raise InstallError(exc.code, exc.message, exc.details) from exc
            raise InstallError(
                "install_failed",
                f"Skill installation failed: {exc}",
                {"id": entity_id, "version": version},
            ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def install_entity(
    entity: dict,
    *,
    channel: str,
    allow_code: Optional[bool] = None,
    source: Optional[source_mod.MarketplaceSource] = None,
    accept_license: bool = False,
    runtime_slug: Optional[str] = None,
) -> dict:
    """Install any catalog entity, dispatching on its type.

    The showcase calls this one function for every card, so the page never
    has to know which installer handles which type. Skills keep their
    original ``install_skill`` behaviour; an agent goes through
    :mod:`core.marketplace.agent_installer`, a base through
    :mod:`core.marketplace.rag_installer` and an MCP server through
    :mod:`core.marketplace.mcp_installer`.
    """
    type_ = str((entity or {}).get("type") or "skill").strip()
    if type_ == "skill":
        return install_skill(
            entity,
            channel=channel,
            allow_code=allow_code,
            source=source,
            accept_license=accept_license,
        )
    if type_ == "agent":
        from core.marketplace.agent_installer import install_agent

        return install_agent(
            entity,
            channel=channel,
            allow_code=allow_code,
            source=source,
            accept_license=accept_license,
            runtime_slug=runtime_slug,
        )
    if type_ == "rag":
        from core.marketplace.rag_installer import install_rag

        return install_rag(
            entity,
            channel=channel,
            allow_code=allow_code,
            source=source,
            accept_license=accept_license,
            runtime_slug=runtime_slug,
        )
    if type_ == "mcp":
        from core.marketplace.mcp_installer import install_mcp

        return install_mcp(
            entity,
            channel=channel,
            allow_code=allow_code,
            source=source,
            accept_license=accept_license,
            runtime_slug=runtime_slug,
        )
    raise InstallError(
        "unsupported_type",
        f"Entities of type '{type_}' cannot be installed here",
        {"type": type_, "id": str((entity or {}).get("id") or "")},
    )


def _fail_run(
    install_id: str,
    runtime_slug: str,
    created_skill_id: str,
    backup_registry: str,
    exc: Exception,
) -> None:
    """Roll back a failed run and record the failure.

    Rollback errors must not mask the original exception: they are recorded
    in the run log, while the caller still receives the real cause. A run
    that never reached the backup stage (failure during download or archive
    validation) has nothing to undo - the staging folder is cleaned by the
    caller's ``finally`` block.
    """
    rolled_back = False
    try:
        meta = store.read_run_meta(install_id)
        if meta is not None:
            rollback.rollback_run(install_id, reason=f"install failed: {exc}")
            rolled_back = True
    except Exception:
        rolled_back = False
    if not rolled_back and created_skill_id:
        # No backup metadata: the import may still have created a record, so
        # remove it directly to leave no orphan behind.
        try:
            from core.skills_library import delete_skill

            delete_skill(created_skill_id)
        except Exception:
            pass
    store.update_run(
        install_id,
        status="failed",
        failed_at=store.isoformat_utc(),
        failure_reason=str(exc),
        rolled_back=rolled_back,
    )
    store.clear_state()


def _display_name(entity: dict) -> str:
    """Return the human name of an entity for the registry record."""
    name = entity.get("name")
    if isinstance(name, dict):
        for lang in ("en", "ru", "zh-CN", "zh_CN"):
            value = str(name.get(lang) or "").strip()
            if value:
                return value
        for value in name.values():
            if str(value).strip():
                return str(value).strip()
    if isinstance(name, str) and name.strip():
        return name.strip()
    return str(entity.get("id") or "skill")


def _display_description(entity: dict) -> str:
    """Return the human description of an entity."""
    description = entity.get("description")
    if isinstance(description, dict):
        for lang in ("en", "ru", "zh-CN", "zh_CN"):
            value = str(description.get(lang) or "").strip()
            if value:
                return value
        for value in description.values():
            if str(value).strip():
                return str(value).strip()
    return str(description or "").strip()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
