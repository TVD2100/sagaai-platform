"""
core.marketplace.agent_orchestrator - installing an orchestrator agent.

An orchestrator package ships a folder: ``orchestrator.json``,
``system_prompt.md``, ``instructions/*.md`` and ``functions/*.py``. Installing
it means the package is verified and unpacked into staging, the files of an
already installed agent are copied aside, the backup metadata goes down, the
importer turns the bundle into a folder and a database row, and the registry
row is written last as the commit marker.

The steps live here as separate functions so a failure can be traced to one
of them; the order they are called in is the contract and must not change.
"""
from __future__ import annotations

import os
import shutil
from typing import Dict, List, Optional

from core.marketplace import store, verify
from core.marketplace.agent_areas import (
    _orchestrator_area_files,
    _remove_stale_area_files,
)
from core.marketplace.agent_package import (
    _apply_config_defaults,
    _bundle_from_folder,
    _merge_untouched,
    _merge_update_config,
    _previous_package_meta,
)
from core.marketplace.agent_preflight import _resolve_slug
from core.marketplace.agent_run import (
    commit_agent_run,
    fail_agent_run,
    open_agent_run,
    register_agent,
    write_agent_run_meta,
)
from core.marketplace.installer import InstallError


def _stage_orchestrator_package(
    catalog,
    entity: dict,
    staging: str,
    declared: Dict[str, str],
    skipped_code: List[str],
    install_code: bool,
) -> tuple:
    """Download, verify and unpack the package; return ``(sha, unpack_dir)``.

    The package is written into staging under its catalog name and cached by
    hash. The cache is best effort - a full cache directory must not fail an
    install that already has the bytes in hand.
    """
    package = catalog.fetch_package(entity)
    package_sha = verify.verify_package(
        package, str((entity.get("package") or {}).get("sha256") or "")
    )
    package_path = os.path.join(staging, "package.tar.gz")
    with open(package_path, "wb") as handle:
        handle.write(package)
    try:
        store.cache_package(package_sha, package)
    except OSError:
        pass

    members = verify.validate_archive(package_path)
    unpack_dir = os.path.join(staging, "content")
    skip_paths = list(skipped_code) if not install_code else []
    verify.extract_archive(package_path, unpack_dir, members, skip=skip_paths)

    mismatched = verify.verify_files(unpack_dir, declared)
    if mismatched:
        raise InstallError(
            "file_hash_mismatch",
            "Unpacked files do not match the manifest",
            {"files": mismatched[:10], "count": len(mismatched)},
        )
    return package_sha, unpack_dir


def _backup_orchestrator_area(
    slug: str,
    install_id: str,
    declared: Dict[str, str],
) -> tuple:
    """Copy the files an update replaces aside; return the run's file lists.

    An update replaces the files of an agent that is already there, so every
    file of the hashing area is copied into the run backup first. Files the
    new version adds did not exist before the run, which is what makes a
    rollback delete them instead of restoring them.
    """
    from core.orchestrator_folders import get_orchestrator_dir

    target_folder = get_orchestrator_dir(slug)
    folder_exists = os.path.isdir(target_folder)
    replaced_files = sorted(_orchestrator_area_files(slug)) if folder_exists else []
    for rel_path in replaced_files:
        store.copy_into_backup(
            os.path.join(target_folder, rel_path), install_id, rel_path
        )
    added_files = sorted(set(declared) - set(replaced_files)) if folder_exists else []
    return folder_exists, replaced_files, added_files


def _merge_orchestrator_update(bundle: dict, existing: Optional[dict], slug: str) -> dict:
    """Keep the values the user changed, move the ones they never touched.

    An update refreshes the entity, not the user's choices: a value the user
    changed stays, a value they never touched follows the package.
    """
    from core.orchestrators import get_orchestrator

    previous = _previous_package_meta(existing)
    installed_row = get_orchestrator(slug) or {}
    bundle["config"] = _merge_update_config(
        installed_row.get("config") or {},
        bundle.get("config") or {},
        previous.get("config") or {},
    )
    bundle["tools"] = _merge_untouched(
        list(installed_row.get("tools") or []),
        list(bundle.get("tools") or []),
        list(previous.get("tools") or []),
    )
    if "max_steps" in previous:
        bundle["max_steps"] = _merge_untouched(
            installed_row.get("max_steps"),
            bundle.get("max_steps"),
            previous["max_steps"],
        )
    if "auto_apply" in previous:
        bundle["auto_apply"] = _merge_untouched(
            installed_row.get("auto_apply"),
            bundle.get("auto_apply"),
            previous["auto_apply"],
        )
    return bundle


def _import_orchestrator_bundle(bundle: dict, slug: str, entity_id: str) -> dict:
    """Hand the bundle to the importer and verify where it landed.

    The importer names the folder itself, so a different slug means the row
    landed somewhere this run did not plan for; recording it would hide the
    mismatch instead of reporting it.
    """
    from core.orchestrators import import_orchestrator

    imported = import_orchestrator(bundle, overwrite=True)
    if not imported.get("ok"):
        raise InstallError(
            "import_failed",
            str(imported.get("error") or "The orchestrator importer refused the bundle"),
            {"id": entity_id},
        )
    created_slug = str(imported.get("slug") or "")
    if created_slug != slug:
        raise InstallError(
            "slug_mismatch",
            f"Importer placed the agent under '{created_slug}', expected '{slug}'",
            {"id": entity_id, "expected": slug, "actual": created_slug},
        )
    return imported


def _verify_orchestrator_area(
    slug: str,
    declared: Dict[str, str],
    skipped_code: List[str],
    folder_exists: bool,
    entity_id: str,
) -> Dict[str, str]:
    """Retire dropped files and return the agent files now on disk.

    The importer rewrites the bundle and the prompt, so a file the package
    shipped may legitimately differ from the live bytes. What must hold is
    that every area file type the package declared exists with a real hash.
    A code file the user chose not to install is kept as it is: the setting
    says "do not install code", not "delete my code".
    """
    if folder_exists:
        keep = set(declared) | set(skipped_code)
        _remove_stale_area_files(slug, keep, "orchestrator")
    files_now = _orchestrator_area_files(slug)
    if not files_now:
        raise InstallError(
            "incomplete_install",
            "The agent folder holds none of the files an agent consists of",
            {"id": entity_id, "runtime_slug": slug},
        )
    absent = sorted(path for path, digest in files_now.items() if not digest)
    if absent:
        raise InstallError(
            "incomplete_install",
            "The agent folder is missing files the manifest declares",
            {"id": entity_id, "files": absent[:10]},
        )
    return files_now


def _install_orchestrator(
    entity: dict,
    preflight: dict,
    *,
    channel: str,
    runtime_slug: Optional[str] = None,
) -> dict:
    """Install an orchestrator entity (an employee) into its folder.

    Parameters:
        entity: parsed ``entity.json`` of the agent.
        preflight: validated values from :func:`_install_preflight`.
        channel: release channel name recorded on the registry row.
        runtime_slug: folder name to use; ``None`` derives it from the
            manifest and appends a suffix when it is taken.

    Any failure after the first change rolls the run back: the created folder
    and the created DB row are removed together and the registry row never
    appears.
    """
    entity_id = preflight["entity_id"]
    version = preflight["version"]
    existing = preflight["existing"]
    declared = preflight["declared"]
    skipped_code = preflight["skipped_code"]
    install_code = preflight["install_code"]
    license_at = preflight["license_at"]
    catalog = preflight["catalog"]

    with store.install_lock():
        install_id, staging, _backup = open_agent_run(
            entity_id, version, agent_kind="orchestrator"
        )
        slug = ""
        try:
            package_sha, unpack_dir = _stage_orchestrator_package(
                catalog, entity, staging, declared, skipped_code, install_code
            )
            slug = _resolve_slug(entity, existing, runtime_slug)
            folder_exists, replaced_files, added_files = _backup_orchestrator_area(
                slug, install_id, declared
            )
            write_agent_run_meta(
                install_id,
                entity_id=entity_id,
                slug=slug,
                folder_exists=folder_exists,
                existing=existing,
                added_files=added_files,
                replaced_files=replaced_files,
            )

            bundle = _bundle_from_folder(unpack_dir, slug)
            if existing is not None and folder_exists:
                bundle = _merge_orchestrator_update(bundle, existing, slug)
            bundle = _apply_config_defaults(bundle, entity, entity_id, version, channel)

            imported = _import_orchestrator_bundle(bundle, slug, entity_id)
            from core.orchestrators import get_orchestrator

            row = get_orchestrator(slug) or {}
            files_now = _verify_orchestrator_area(
                slug, declared, skipped_code, folder_exists, entity_id
            )
            tree = register_agent(
                entity=entity,
                entity_id=entity_id,
                slug=slug,
                version=version,
                channel=channel,
                package_sha=package_sha,
                files_now=files_now,
                skipped_code=skipped_code,
                install_id=install_id,
                license_at=license_at,
                agent_kind="orchestrator",
                runtime_db_id=str(row.get("id") or ""),
            )
            commit_agent_run(
                install_id, slug=slug, entity_id=entity_id, agent_kind="orchestrator"
            )

            return {
                "ok": True,
                "noop": False,
                "id": entity_id,
                "type": "agent",
                "agent_kind": "orchestrator",
                "version": version,
                "install_id": install_id,
                "runtime_slug": slug,
                "orchestrator_id": str(row.get("id") or ""),
                "tree_sha256": tree,
                "skipped_code_files": skipped_code,
                "instructions_imported": int(imported.get("instructions_imported") or 0),
                "functions_imported": int(imported.get("functions_imported") or 0),
                "tools_rejected": list(imported.get("tools_rejected") or []),
            }
        except Exception as exc:
            fail_agent_run(install_id, slug, exc, agent_kind="orchestrator")
            if isinstance(exc, InstallError):
                raise
            if isinstance(exc, store.MarketplaceError):
                raise InstallError(exc.code, exc.message, exc.details) from exc
            raise InstallError(
                "install_failed",
                f"Agent installation failed: {exc}",
                {"id": entity_id, "version": version},
            ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
