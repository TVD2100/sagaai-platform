"""
core.marketplace.agent_assistant - installing an assistant agent.

A helper package ships ``manifest.json`` (metadata), ``prompt.md`` (the system
prompt) and optional ``files/*`` attachments. The folder importer rebuilds both
the folder and the database row from the payload, so the run prepares the
payload and records its backup metadata before the import.

Attachments the user added themselves are read before the import and put back
after it: they are outside the entity, an update must not lose them, and the
importer rebuilds ``files/`` from the payload.
"""
from __future__ import annotations

import os
import shutil
from typing import Dict, List, Optional

from core.marketplace import store, verify
from core.marketplace.agent_areas import _assistant_area_files
from core.marketplace.agent_package import (
    _assistant_bundle_from_folder,
    _merge_assistant_payload,
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


def _check_assistant_attachments(declared: Dict[str, str], entity_id: str) -> None:
    """Refuse a package whose attachments the storage layer cannot keep.

    The storage layer keeps attachments as ``.txt``; a package that ships
    anything else is refused before the download rather than landing
    half-installed.
    """
    unsupported = sorted(
        path for path in declared
        if path.startswith("files/") and not path.endswith(".txt")
    )
    if unsupported:
        raise InstallError(
            "unsupported_attachment",
            "Assistant attachments must be text files (.txt)",
            {"id": entity_id, "files": unsupported[:10]},
        )


def _stage_assistant_package(
    catalog,
    entity: dict,
    staging: str,
    declared: Dict[str, str],
    skipped_code: List[str],
    install_code: bool,
) -> tuple:
    """Download, verify and unpack the package; return ``(sha, unpack_dir)``."""
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


def _plan_assistant_attachments(
    slug: str,
    install_id: str,
    folder_exists: bool,
    existing: Optional[dict],
    declared: Dict[str, str],
) -> tuple:
    """Back up the helper area and collect the attachments to keep.

    Attachments the user added are copied into the run backup and put back
    after the import; the backup also restores them when the run fails.
    """
    from core.assistant_folders import (
        get_assistant_dir,
        list_assistant_files,
        load_assistant_file_content,
    )

    folder = get_assistant_dir(slug)
    recorded = dict((existing or {}).get("files_json") or {})
    replaced_files = sorted(_assistant_area_files(slug, recorded)) if folder_exists else []
    package_attachments = {
        path.split("/", 1)[1] for path in declared if path.startswith("files/")
    }
    preserved: Dict[str, str] = {}
    if folder_exists:
        for fname in list_assistant_files(slug):
            if fname in package_attachments:
                continue
            preserved[f"files/{fname}"] = load_assistant_file_content(slug, fname)
    for rel_path in sorted(set(replaced_files) | set(preserved)):
        store.copy_into_backup(os.path.join(folder, rel_path), install_id, rel_path)
    added_files = sorted(set(declared) - set(replaced_files)) if folder_exists else []
    return folder, replaced_files, added_files, preserved, package_attachments


def _prepare_assistant_payload(
    payload: dict,
    entity: dict,
    entity_id: str,
    version: str,
    channel: str,
    existing: Optional[dict],
    slug: str,
    folder_exists: bool,
) -> dict:
    """Merge the installed values an update keeps and stamp the marker.

    The marker records which catalog entity the profile came from, which is
    what a later update and the removal marker key on.
    """
    if existing is not None and folder_exists:
        from core.assistants import get_assistant_by_id, get_assistant_by_slug

        installed_row = get_assistant_by_slug(slug) or {}
        if installed_row and not installed_row.get("text"):
            installed_row = get_assistant_by_id(
                str(installed_row.get("id") or "")
            ) or installed_row
        previous = _previous_package_meta(
            existing, names=("manifest.json", "settings.json")
        )
        payload = _merge_assistant_payload(
            payload, installed_row, previous, existing
        )
    bundle = dict(payload.get("bundle") or {})
    defaults = entity.get("config_defaults")
    if isinstance(defaults, dict):
        for key, value in defaults.items():
            bundle.setdefault(str(key), value)
    bundle["marketplace"] = {
        "id": entity_id,
        "version": version,
        "channel": channel,
        "installed_at": store.isoformat_utc(),
    }
    payload["bundle"] = bundle
    return payload


def _import_assistant_folder(
    slug: str,
    payload: dict,
    preserved: Dict[str, str],
    entity_id: str,
) -> str:
    """Write the folder, put the kept attachments back and reload the row.

    Returns the database id of the reloaded profile. The folder importer
    rebuilds ``files/`` from the payload, so the attachments the user added
    are written back afterwards.
    """
    from core.assistant_folders import import_assistant_folder, save_assistant_file
    from core.assistants import reload_assistant_from_folder

    if not import_assistant_folder(slug, payload):
        raise InstallError(
            "import_failed",
            "The assistant folder could not be written",
            {"id": entity_id, "runtime_slug": slug},
        )
    for rel_path, content in preserved.items():
        save_assistant_file(slug, rel_path.split("/", 1)[1], content)

    reloaded = reload_assistant_from_folder(slug)
    if not reloaded.get("ok"):
        raise InstallError(
            "import_failed",
            str(reloaded.get("error") or "The assistant importer refused the folder"),
            {"id": entity_id, "runtime_slug": slug},
        )
    return str(reloaded.get("id") or "")


def _verify_assistant_area(
    slug: str,
    folder: str,
    replaced_files: List[str],
    declared: Dict[str, str],
    skipped_code: List[str],
    preserved: Dict[str, str],
    folder_exists: bool,
    entity_id: str,
) -> Dict[str, str]:
    """Retire dropped files and return the helper files now on disk.

    An update retires what the new version no longer ships. Only the files
    the previous version recorded are candidates: a file the user added is
    theirs.
    """
    if folder_exists:
        keep = set(declared) | set(skipped_code) | set(preserved)
        for rel_path in replaced_files:
            if rel_path in keep:
                continue
            full = os.path.join(folder, rel_path)
            if os.path.isfile(full):
                try:
                    os.remove(full)
                except OSError:
                    pass
    files_now = _assistant_area_files(slug, declared)
    if not files_now:
        raise InstallError(
            "incomplete_install",
            "The assistant folder holds none of the files a helper consists of",
            {"id": entity_id, "runtime_slug": slug},
        )
    absent = sorted(path for path, digest in files_now.items() if not digest)
    if absent:
        raise InstallError(
            "incomplete_install",
            "The assistant folder is missing files the manifest declares",
            {"id": entity_id, "files": absent[:10]},
        )
    return files_now


def _install_assistant(
    entity: dict,
    preflight: dict,
    *,
    channel: str,
    runtime_slug: Optional[str] = None,
) -> dict:
    """Install a helper entity (an assistant profile) into its folder.

    A helper lives in ``DATA_DIR/assistants/<slug>``: ``manifest.json`` with
    the metadata, ``prompt.md`` with the system prompt and ``files/*`` with
    the attachments the package ships. The folder importer rebuilds both the
    folder and the database row from the payload, so the run prepares the
    payload and records its backup metadata before the import.

    Attachments the user added themselves are read before the import and put
    back after it: they are outside the entity, an update must not lose them,
    and the importer rebuilds ``files/`` from the payload.
    """
    entity_id = preflight["entity_id"]
    version = preflight["version"]
    existing = preflight["existing"]
    declared = preflight["declared"]
    skipped_code = preflight["skipped_code"]
    install_code = preflight["install_code"]
    license_at = preflight["license_at"]
    catalog = preflight["catalog"]

    _check_assistant_attachments(declared, entity_id)

    with store.install_lock():
        install_id, staging, _backup = open_agent_run(
            entity_id, version, agent_kind="assistant"
        )
        slug = ""
        try:
            package_sha, unpack_dir = _stage_assistant_package(
                catalog, entity, staging, declared, skipped_code, install_code
            )
            slug = _resolve_slug(entity, existing, runtime_slug, "assistant")
            from core.assistant_folders import get_assistant_dir

            folder_exists = os.path.isdir(get_assistant_dir(slug))
            folder, replaced_files, added_files, preserved, package_attachments = (
                _plan_assistant_attachments(
                    slug, install_id, folder_exists, existing, declared
                )
            )
            write_agent_run_meta(
                install_id,
                entity_id=entity_id,
                slug=slug,
                folder_exists=folder_exists,
                existing=existing,
                added_files=added_files,
                replaced_files=sorted(set(replaced_files) | set(preserved)),
                agent_kind="assistant",
            )

            payload = _assistant_bundle_from_folder(unpack_dir, slug)
            payload = _prepare_assistant_payload(
                payload, entity, entity_id, version, channel, existing, slug, folder_exists
            )
            assistant_id = _import_assistant_folder(slug, payload, preserved, entity_id)
            files_now = _verify_assistant_area(
                slug, folder, replaced_files, declared, skipped_code, preserved,
                folder_exists, entity_id,
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
                agent_kind="assistant",
                runtime_db_id=assistant_id,
            )
            commit_agent_run(
                install_id,
                slug=slug,
                entity_id=entity_id,
                agent_kind="assistant",
                extra={"assistant_id": assistant_id},
            )

            return {
                "ok": True,
                "noop": False,
                "id": entity_id,
                "type": "agent",
                "agent_kind": "assistant",
                "version": version,
                "install_id": install_id,
                "runtime_slug": slug,
                "assistant_id": assistant_id,
                "tree_sha256": tree,
                "skipped_code_files": skipped_code,
                "attachments": sorted(package_attachments),
                "preserved_attachments": sorted(
                    path.split("/", 1)[1] for path in preserved
                ),
            }
        except Exception as exc:
            fail_agent_run(install_id, slug, exc, agent_kind="assistant")
            if isinstance(exc, InstallError):
                raise
            if isinstance(exc, store.MarketplaceError):
                raise InstallError(exc.code, exc.message, exc.details) from exc
            raise InstallError(
                "install_failed",
                f"Assistant installation failed: {exc}",
                {"id": entity_id, "version": version},
            ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
