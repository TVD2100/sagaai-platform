"""
core.marketplace.conflicts - what happens when an entity meets a local state.

The installer knows how to put a package on disk. This module decides
whether it should: every install request goes through ``resolve_plan()``
first, which turns the catalog entity plus the local state into one of the
outcomes of the conflict matrix (architecture §4.1, rows 1-8 and 10).
Row 9 - conflicting constraints on a shared dependency - needs the resolver
and the ``depends`` field and belongs to the agent package.

Two ideas keep the rules honest:

  * identity is the catalog ``id`` in ``marketplace_installed``, never the
    folder name and never the display name;
  * "modified" means "a file the package declares has different content" -
    a note the user drops next to ``SKILL.md`` is theirs and is invisible to
    the detector, because the hashing area is the installed file manifest
    (D-04).

When the user has edited files, nothing is replaced without a choice: the
three paths below are ``keep_mine`` (update only what is untouched),
``take_catalog`` (back up the user's files, then replace) and ``fork``
(keep both by installing a copy under its own id).
"""
from __future__ import annotations

import os
import shutil
from typing import Dict, List, Optional, Tuple

from core.marketplace import config as marketplace_config
from core.marketplace import installer, registry, semver, source as source_mod, store, verify


CHOICES = ("keep_mine", "take_catalog", "fork")


class ConflictError(store.MarketplaceError):
    """Raised when a conflict cannot be resolved as requested."""


# ─── Local state helpers ──────────────────────────────────────────────────

def entity_folder(type_: str, runtime_slug: str) -> Optional[str]:
    """Return the folder an entity occupies or would occupy, existing or not."""
    if not runtime_slug:
        return None
    if type_ == "skill":
        return os.path.join(registry.skills_root(), runtime_slug)
    if type_ == "rag":
        return os.path.join(registry.rag_root(), runtime_slug)
    if type_ == "agent":
        # An agent lives either in its orchestrator folder or in its helper
        # folder under ``assistants/``; the registry knows which one and the
        # defaults importer writes to the same place - one location, one
        # source of truth.
        return registry.agent_folder(runtime_slug)
    return None


def _slug_owner(type_: str, runtime_slug: str) -> Optional[dict]:
    """Return the registry row that owns *runtime_slug*, or None.

    This is the lookup that separates "the same entity, registry lost"
    (adopt) from "another entity already sits there" (suffix): matching by
    folder name alone cannot tell them apart.
    """
    if not runtime_slug:
        return None
    for row in registry.list_installed(type_):
        if str(row.get("runtime_slug") or "") == runtime_slug:
            return row
    return None


def detect_local_modifications(type_: str, runtime_slug: str) -> dict:
    """Compare the files on disk with the manifest recorded at install time.

    Only files the package declares take part: ``SKILL.md`` and friends for
a skill. Files outside the manifest are user data - adding one is not a
    modification, and a file listed in the manifest but gone from disk is
    reported as changed with an empty hash so the caller can tell.

    Returns ``{"modified": bool, "files": [rel_path, ...], "missing": [...]}``.
    """
    row = registry.get_installed(type_, registry.runtime_slug_id(type_, runtime_slug))
    if row is None or not row.get("files_json"):
        return {"modified": False, "files": [], "missing": []}
    recorded: Dict[str, str] = dict(row.get("files_json") or {})
    actual = registry.entity_file_manifest(type_, runtime_slug, recorded)
    changed: List[str] = []
    missing: List[str] = []
    for rel_path, digest in recorded.items():
        current = actual.get(rel_path)
        if current == digest:
            continue
        changed.append(rel_path)
        if not current:
            missing.append(rel_path)
    user_marked = list(row.get("user_modified_files_json") or [])
    for rel_path in user_marked:
        if rel_path not in changed:
            changed.append(rel_path)
    return {
        "modified": bool(changed),
        "files": sorted(changed),
        "missing": sorted(missing),
    }


def local_state(entity: dict, *, type_: str = "skill") -> dict:
    """Return the local state of a catalog entity.

    The result carries exactly what the matrix needs: whether the catalog id
    is installed and at which version, whether its folder exists without a
    registry row (a legacy orphan worth adopting), whether the folder is
    taken by another entity and how badly the files were edited.
    """
    entity_id = str(entity.get("id") or "")
    catalog_version = str(entity.get("version") or "0.0.0")
    row = registry.get_installed(type_, entity_id) if entity_id else None
    runtime_slug = str((row or {}).get("runtime_slug") or entity.get("runtime_slug") or entity_id)
    folder = entity_folder(type_, runtime_slug)
    folder_exists = bool(folder and os.path.isdir(folder))
    owner = _slug_owner(type_, runtime_slug)

    changes = {"modified": False, "files": [], "missing": []}
    if row is not None:
        changes = detect_local_modifications(type_, runtime_slug)

    return {
        "id": entity_id,
        "catalog_version": catalog_version,
        "installed": row is not None,
        "row": row,
        "installed_version": str((row or {}).get("version") or "") or None,
        "runtime_slug": runtime_slug,
        "folder_exists": folder_exists,
        "slug_owner": owner,
        "slug_taken_by_other": bool(owner is not None and str(owner.get("id")) != entity_id),
        "user_modified": bool((row or {}).get("user_modified")) or changes["modified"],
        "modified_files": changes["files"],
        "missing_files": changes["missing"],
    }


# ─── The matrix ───────────────────────────────────────────────────────────

def resolve_plan(
    entity: dict,
    *,
    type_: str = "skill",
    force_downgrade: bool = False,
    accept_license: bool = False,
) -> dict:
    """Decide what an install request should do, without touching the disk.

    Returns a plan dict whose ``action`` is one of:

    ``install``          nothing installed - row 1
    ``noop``             same version, files untouched - row 2
    ``modified_hold``    same version, files edited - row 3, offers a reset
    ``update``           older version installed - row 4
    ``downgrade_blocked`` newer version installed - row 5, needs a forced run
    ``conflict_choice``  files edited and a different version - row 6
    ``suffix_install``   the folder belongs to another entity - row 7
    ``adopt``            folder matches the catalog but the registry lost it - row 8

    ``license_required`` and ``platform_incompatible`` are refusals that
    happen before any of the rows: they would fail the same way whatever
    the local state is.
    """
    if not isinstance(entity, dict) or not entity.get("id"):
        raise ConflictError("invalid_entity", "Entity manifest has no id")

    state = local_state(entity, type_=type_)
    entity_id = state["id"]
    catalog_version = state["catalog_version"]
    licence = entity.get("license") if isinstance(entity.get("license"), dict) else {}
    if licence.get("acceptance_required") and not accept_license:
        return {
            "action": "license_required",
            "id": entity_id,
            "version": catalog_version,
            "spdx": licence.get("spdx"),
            "message": "The entity requires accepting its licence",
            "state": state,
        }

    compatible, reason = installer._platform_compatible(entity)
    if not compatible:
        return {
            "action": "platform_incompatible",
            "id": entity_id,
            "version": catalog_version,
            "message": reason,
            "state": state,
        }

    # Row 1: nothing installed under this id.
    if not state["installed"]:
        if state["folder_exists"] and not state["slug_taken_by_other"]:
            if _adoptable(entity, state["runtime_slug"], type_):
                return _plan("adopt", entity, state, message="Found an existing installation")
        if state["slug_taken_by_other"]:
            return _plan("suffix_install", entity, state, message="The folder name is taken")
        return _plan("install", entity, state)

    # Row 7 comes before the version arithmetic: a row that names another
    # entity describes a different installation entirely.
    if state["slug_taken_by_other"]:
        return _plan("suffix_install", entity, state, message="The folder name is taken")

    installed_version = state["installed_version"] or "0.0.0"
    try:
        comparison = semver.compare(installed_version, catalog_version)
    except semver.SemverError as exc:
        raise ConflictError(
            "invalid_version",
            f"Entity version is not valid semver: {catalog_version}",
            {"id": entity_id, "version": catalog_version},
        ) from exc

    if comparison > 0 and not force_downgrade:
        # Row 5: a downgrade destroys data produced by the newer version, so
        # it only happens after an explicit confirmation.
        return _plan(
            "downgrade_blocked",
            entity,
            state,
            message=f"Version {catalog_version} is older than the installed {installed_version}",
        )

    if comparison == 0:
        if state["user_modified"]:
            # Row 3: same version, edited files. Nothing to install; the user
            # may reset to the catalog copy from the package cache.
            return _plan(
                "modified_hold",
                entity,
                state,
                message="Installed and modified by you",
            )
        return _plan("noop", entity, state, message="Already installed")

    if state["user_modified"]:
        # Row 6: files were edited and the catalog has another version - the
        # user has to pick one of the three paths.
        return _plan(
            "conflict_choice",
            entity,
            state,
            message="The entity has changed and your files differ",
        )

    return _plan("update", entity, state)


def _plan(action: str, entity: dict, state: dict, *, message: str = "") -> dict:
    """Build a plan dict from a matrix row and the local state."""
    return {
        "action": action,
        "id": state["id"],
        "version": state["catalog_version"],
        "installed_version": state["installed_version"],
        "runtime_slug": state["runtime_slug"],
        "user_modified": state["user_modified"],
        "modified_files": list(state["modified_files"]),
        "message": message,
        "state": state,
    }


def _adoptable(entity: dict, runtime_slug: str, type_: str) -> bool:
    """True when an existing folder holds this very entity, not a stranger.

    The check compares the folder against the catalog manifest. It is the
    only evidence available when the registry row was lost, and a mismatch
    must NOT be adopted silently: the caller falls back to the suffix path
    or to the three-way choice instead.
    """
    declared = installer._entity_files(entity)
    if not declared:
        return False
    installed = registry.entity_file_manifest(type_, runtime_slug, declared)
    for rel_path, digest in declared.items():
        if installed.get(rel_path) != str(digest):
            return False
    return True


# ─── Adopt ────────────────────────────────────────────────────────────────

def adopt(entity: dict, *, channel: str, type_: str = "skill") -> dict:
    """Register an installation that already exists on disk.

    Used when the folder matches the catalog manifest but no registry row
    survived (a restored backup, a manually copied skill). Nothing is
    downloaded or replaced: the row is rebuilt from what is on disk, which
    is why the tree hash is computed from the files rather than copied from
    the manifest.
    """
    state = local_state(entity, type_=type_)
    runtime_slug = state["runtime_slug"]
    if not state["folder_exists"]:
        raise ConflictError(
            "nothing_to_adopt",
            f"No folder to adopt for {state['id']}",
            {"id": state["id"], "runtime_slug": runtime_slug},
        )
    files = registry.entity_file_manifest(type_, runtime_slug, installer._entity_files(entity))
    if not files:
        raise ConflictError("nothing_to_adopt", "The folder holds no declared files")
    now = store.isoformat_utc()
    skill_id = _skill_id_of(runtime_slug)
    record = {
        "type": type_,
        "id": state["id"],
        "runtime_slug": runtime_slug,
        "runtime_db_id": None,
        "runtime_skill_id": skill_id,
        "fork_of": entity.get("fork_of"),
        "version": state["catalog_version"],
        "channel": channel,
        "source_url": str(entity.get("source_url") or ""),
        "package_sha256": str((entity.get("package") or {}).get("sha256") or "") or None,
        "tree_sha256": registry.tree_sha256(type_, runtime_slug, files),
        "index_sha256": None,
        "files_json": files,
        "skipped_code_files_json": [],
        "installed_at": now,
        "updated_at": now,
        "installed_by": "marketplace",
        "user_modified": False,
        "user_modified_files_json": [],
        "install_id": None,
        "bundle_id": entity.get("bundle_id"),
        "pinned": False,
        "license_accepted_at": None,
    }
    if type_ == "agent":
        # The adopted installation must remember which kind it is: an update
        # and a removal then find its folder without probing both roots.
        record["agent_kind"] = str(entity.get("agent_kind") or "orchestrator")
    registry.upsert_installed(record)
    registry.unmark_removed(type_, state["id"])
    registry.export_registry_cache()
    return {"ok": True, "adopted": True, "id": state["id"], "runtime_slug": runtime_slug}


def _skill_id_of(runtime_slug: str) -> Optional[str]:
    """Return the 8-hex skills-library id of a folder, or None."""
    try:
        from core.skills_library import list_skills

        for skill in list_skills():
            if str(skill.get("folder") or "") == runtime_slug:
                return str(skill.get("id") or "") or None
    except Exception:
        return None
    return None


# ─── Three paths ──────────────────────────────────────────────────────────

def resolve_three_way(
    choice: str,
    entity: dict,
    *,
    channel: str,
    type_: str = "skill",
    source: Optional[source_mod.MarketplaceSource] = None,
    allow_code: Optional[bool] = None,
    accept_license: bool = False,
) -> dict:
    """Apply one of the three conflict paths and return a report.

    ``keep_mine`` updates only the files the user did not touch and keeps
    the edited ones, recording them so the card can say so and a later
    ``take_catalog`` knows what to back up.

    ``take_catalog`` copies the edited files into the run backup first, then
    installs the catalog version in full; ``restore_user_version()`` brings
    the user's bytes back.

    ``fork`` installs the catalog version under ``<id>#forkN`` and leaves the
    edited folder alone, which is the only path that keeps both copies.
    """
    if choice not in CHOICES:
        raise ConflictError(
            "invalid_choice",
            f"Unknown conflict choice: {choice}",
            {"choices": list(CHOICES)},
        )
    if type_ != "skill":
        raise ConflictError(
            "unsupported_type",
            "Only skills implement the three paths in this slice",
            {"type": type_},
        )
    if choice == "fork":
        return _fork(entity, channel=channel, source=source, allow_code=allow_code,
                     accept_license=accept_license)
    return _apply_choice(choice, entity, channel=channel, source=source,
                         allow_code=allow_code, accept_license=accept_license)


def _apply_choice(
    choice: str,
    entity: dict,
    *,
    channel: str,
    source: Optional[source_mod.MarketplaceSource],
    allow_code: Optional[bool],
    accept_license: bool,
) -> dict:
    """Run ``keep_mine`` or ``take_catalog`` inside one transactional run."""
    settings = marketplace_config.marketplace_settings()
    if not settings.get("enabled"):
        raise ConflictError("marketplace_disabled", "The marketplace is disabled in the settings")

    state = local_state(entity, type_="skill")
    row = state["row"] or {}
    runtime_slug = state["runtime_slug"]
    folder = entity_folder("skill", runtime_slug)
    if not folder or not os.path.isdir(folder):
        raise ConflictError(
            "not_installed",
            "The three paths apply to an installed entity",
            {"id": state["id"]},
        )

    catalog_files = installer._entity_files(entity)
    install_code = settings["allow_code"] if allow_code is None else bool(allow_code)
    non_code, code_files = installer._split_code_files(catalog_files)
    target_files = dict(catalog_files) if install_code else dict(non_code)
    skipped_code = [] if install_code else sorted(code_files)
    if not target_files:
        raise ConflictError("nothing_to_install", "Every file of the entity is executable")

    modified = set(detect_local_modifications("skill", runtime_slug)["files"])
    replaced = sorted(target_files) if choice == "take_catalog" else sorted(
        path for path in target_files if path not in modified
    )
    kept = sorted(path for path in target_files if path in modified) if choice == "keep_mine" else []
    if choice == "take_catalog" and not modified:
        # Nothing was edited - the caller should have chosen an update, but
        # doing it here keeps the function total.
        kept = []

    catalog = source or source_mod.default_source()
    install_id = ""
    with store.install_lock():
        install_id = store.new_install_id()
        staging, _backup = store.prepare_run_dirs(install_id)
        store.append_run(
            {
                "install_id": install_id,
                "type": "skill",
                "id": state["id"],
                "version": state["catalog_version"],
                "action": choice,
                "status": "applying",
                "from_version": state["installed_version"],
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
            try:
                store.cache_package(package_sha, package)
            except OSError:
                pass
            members = verify.validate_archive(package_path)
            unpack_dir = os.path.join(staging, "content")
            verify.extract_archive(
                package_path,
                unpack_dir,
                members,
                skip=skipped_code if not install_code else [],
            )
            mismatched = verify.verify_files(unpack_dir, target_files)
            if mismatched:
                raise ConflictError(
                    "file_hash_mismatch",
                    "Unpacked files do not match the manifest",
                    {"files": mismatched[:10]},
                )

            backup_registry = installer._backup_registry_file(install_id)
            # Every file the run is about to replace goes into the backup, not
            # only the edited ones: a rollback restores files from this folder,
            # and a replacement it cannot find there is reported as
            # unrestored. ``user_backup_files`` stays the edited subset, which
            # is what "bring my version back" puts in place.
            backed_up: List[str] = []
            for rel_path in replaced:
                candidate = os.path.join(folder, rel_path)
                if os.path.isfile(candidate):
                    store.copy_into_backup(candidate, install_id, rel_path)
                    backed_up.append(rel_path)
            backup_files = [path for path in backed_up if path in set(modified)]
            store.write_run_meta(
                install_id,
                {
                    "install_id": install_id,
                    "type": "skill",
                    "id": state["id"],
                    "runtime_slug": runtime_slug,
                    "created_folder": False,
                    "created_files": [],
                    "replaced_files": replaced,
                    "removed_files": [],
                    "previous_row": row or None,
                    "registry_path": installer._skills_registry_path(),
                    "registry_backup": backup_registry,
                    "registry_existed": bool(backup_registry),
                    "user_backup_files": backup_files,
                },
            )

            for rel_path in replaced:
                target = os.path.join(folder, rel_path)
                os.makedirs(os.path.dirname(target) or folder, exist_ok=True)
                source_file = os.path.join(unpack_dir, rel_path)
                store._atomic_replace(source_file, target)

            files_now = registry.entity_file_manifest("skill", runtime_slug, target_files)
            tree = registry.tree_sha256("skill", runtime_slug, target_files)
            now = store.isoformat_utc()
            installer._write_skill_metadata(
                str(row.get("runtime_skill_id") or ""),
                version=state["catalog_version"],
                updated_at=now,
                source=f"marketplace/{state['id']}",
                sha256=package_sha,
                source_url=str(entity.get("source_url") or ""),
            )
            updated_row = dict(row)
            updated_row.update({
                "version": state["catalog_version"],
                "package_sha256": package_sha,
                "tree_sha256": tree,
                "files_json": files_now,
                "skipped_code_files_json": skipped_code,
                "updated_at": now,
                "user_modified": bool(kept),
                "user_modified_files_json": kept,
                "install_id": install_id,
                "license_accepted_at": row.get("license_accepted_at") or (
                    now if accept_license else None
                ),
            })
            registry.upsert_installed(updated_row)
            store.update_run(
                install_id,
                status="done",
                finished_at=store.isoformat_utc(),
                runtime_slug=runtime_slug,
                kept_files=kept,
                replaced_files=replaced,
            )
            store.cleanup_run_dirs(install_id, keep_backup=True)
            store.clear_state()
            registry.export_registry_cache()
            return {
                "ok": True,
                "choice": choice,
                "id": state["id"],
                "version": state["catalog_version"],
                "install_id": install_id,
                "runtime_slug": runtime_slug,
                "replaced_files": replaced,
                "kept_files": kept,
                "backed_up_files": backup_files,
                "user_modified": bool(kept),
            }
        except Exception as exc:
            _fail(install_id, row, runtime_slug, str(row.get("runtime_skill_id") or ""), exc)
            if isinstance(exc, ConflictError):
                raise
            if isinstance(exc, store.MarketplaceError):
                raise ConflictError(exc.code, exc.message, exc.details) from exc
            raise ConflictError(
                "conflict_failed",
                f"Could not resolve the conflict: {exc}",
                {"id": state["id"], "choice": choice},
            ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _fail(install_id: str, row: dict, runtime_slug: str, skill_id: str, exc: Exception) -> None:
    """Roll a failed conflict run back to the pre-run state."""
    from core.marketplace import rollback

    rolled_back = False
    try:
        if store.read_run_meta(install_id) is not None:
            rollback.rollback_run(install_id, reason=f"conflict failed: {exc}")
            rolled_back = True
    except Exception:
        rolled_back = False
    store.update_run(
        install_id,
        status="failed",
        failed_at=store.isoformat_utc(),
        failure_reason=str(exc),
        rolled_back=rolled_back,
    )
    store.clear_state()


# ─── Fork ─────────────────────────────────────────────────────────────────

def next_fork_id(entity_id: str) -> str:
    """Return the first free ``<id>#forkN`` for an entity."""
    counter = 1
    while True:
        candidate = f"{entity_id}#fork{counter}"
        if registry.get_installed("skill", candidate) is None:
            return candidate
        counter += 1


def next_fork_slug(runtime_slug: str) -> str:
    """Return the first free ``<slug>_fork_NN`` folder name."""
    counter = 1
    while True:
        candidate = f"{runtime_slug}_fork_{counter:02d}"
        if not os.path.isdir(os.path.join(registry.skills_root(), candidate)):
            return candidate
        counter += 1


def _fork(
    entity: dict,
    *,
    channel: str,
    source: Optional[source_mod.MarketplaceSource],
    allow_code: Optional[bool],
    accept_license: bool,
) -> dict:
    """Copy the edited installation aside, then bring the original to the catalog.

    The fork keeps the user's files under a new id and folder; the original
    is resolved by ``take_catalog``, so afterwards both the catalog version
    and the edited copy exist and neither overwrote the other.
    """
    state = local_state(entity, type_="skill")
    if not state["installed"]:
        raise ConflictError(
            "not_installed",
            "Forking applies to an installed entity",
            {"id": state["id"]},
        )
    original_slug = state["runtime_slug"]
    original_folder = entity_folder("skill", original_slug)
    if not original_folder or not os.path.isdir(original_folder):
        raise ConflictError("not_installed", "The installation folder is missing")

    fork_id = next_fork_id(state["id"])
    fork_slug = next_fork_slug(original_slug)
    fork_folder = entity_folder("skill", fork_slug)
    shutil.copytree(original_folder, fork_folder)

    files = registry.entity_file_manifest("skill", fork_slug, installer._entity_files(entity))
    tree = registry.tree_sha256("skill", fork_slug, files)
    now = store.isoformat_utc()
    fork_skill_id = _clone_skill_record(original_slug, fork_slug, fork_id)
    fork_row = dict(state["row"] or {})
    fork_row.update({
        "id": fork_id,
        "runtime_slug": fork_slug,
        "runtime_skill_id": fork_skill_id,
        "fork_of": state["id"],
        "files_json": files,
        "tree_sha256": tree,
        "installed_at": now,
        "updated_at": now,
        "user_modified": True,
        "user_modified_files_json": list(state["modified_files"]),
        "pinned": True,
        "install_id": None,
    })
    registry.upsert_installed(fork_row)

    try:
        applied = _apply_choice(
            "take_catalog",
            entity,
            channel=channel,
            source=source,
            allow_code=allow_code,
            accept_license=accept_license,
        )
    except Exception:
        # The fork is already a separate installation; leaving it in place is
        # better than removing the user's only copy of their edits.
        registry.export_registry_cache()
        raise
    return {
        "ok": True,
        "choice": "fork",
        "id": state["id"],
        "fork_id": fork_id,
        "fork_slug": fork_slug,
        "version": state["catalog_version"],
        "install_id": applied.get("install_id"),
        "fork_install_id": None,
    }


def _clone_skill_record(original_slug: str, fork_slug: str, fork_id: str) -> Optional[str]:
    """Register a second ``skills.json`` record for the fork folder.

    The skills library keys records by an 8-hex id and ``register_skill``
    takes the display fields plus the folder, so the fork gets its own id
    while keeping the original name (marked as a fork) and description.
    """
    from core.skills_library import _load_registry, register_skill

    data = _load_registry()
    for skill_id, record in data.items():
        if str(record.get("folder") or "") != original_slug:
            continue
        name = str(record.get("name") or fork_slug)
        try:
            created = register_skill(f"{name} (fork)", str(record.get("description") or ""), fork_slug)
        except Exception:
            return skill_id
        return str(created or skill_id)
    return None


# ─── Restoring the user's files ───────────────────────────────────────────

def restore_user_version(install_id: str) -> dict:
    """Put the files a ``take_catalog`` run backed up back in place.

    This is the "bring my version back" action of the UI. It reads the run
    backup directly instead of going through ``rollback_run``, because the
    user wants their edited files back - not the whole run undone.
    """
    meta = store.read_run_meta(install_id)
    if meta is None:
        raise ConflictError(
            "missing_run_meta",
            f"No backup metadata found for run {install_id}",
            {"install_id": install_id},
        )
    runtime_slug = str(meta.get("runtime_slug") or "")
    folder = entity_folder(str(meta.get("type") or "skill"), runtime_slug)
    if not folder:
        raise ConflictError("not_installed", "The entity folder is unknown")
    restored: List[str] = []
    missing: List[str] = []
    for rel_path in meta.get("user_backup_files") or []:
        source_file = os.path.join(store.backup_dir(install_id), "files", rel_path)
        if not os.path.isfile(source_file):
            missing.append(rel_path)
            continue
        target = os.path.join(folder, rel_path)
        os.makedirs(os.path.dirname(target) or folder, exist_ok=True)
        shutil.copy2(source_file, target)
        restored.append(rel_path)

    if restored:
        row = registry.get_installed(str(meta.get("type") or "skill"), str(meta.get("id") or ""))
        if row is not None:
            marked = sorted(set(list(row.get("user_modified_files_json") or []) + restored))
            registry.update_installed(
                row["type"],
                row["id"],
                user_modified=True,
                user_modified_files_json=marked,
            )
    return {
        "ok": not missing,
        "install_id": install_id,
        "restored_files": restored,
        "missing": missing,
    }


def reset_to_catalog(
    entity: dict,
    *,
    type_: str = "skill",
    allow_code: Optional[bool] = None,
) -> dict:
    """Put the catalog files back over a locally edited installation.

    This is the "reset to the catalog version" action for row 3 of the
    matrix: the version is the same, only the user's files differ. The
    reference copy comes from the package cache, so no network call is made
    and the operation works offline - but only while the package is still
    cached (D-08). When it is not, the caller gets ``package_not_cached``
    and the UI says the reset is unavailable rather than pretending.

    The edited files are backed up first, so ``restore_user_version()`` can
    bring them back exactly as they were.
    """
    settings = marketplace_config.marketplace_settings()
    if not settings.get("enabled"):
        raise ConflictError("marketplace_disabled", "The marketplace is disabled in the settings")

    state = local_state(entity, type_=type_)
    row = state["row"]
    if row is None:
        raise ConflictError(
            "not_installed",
            "Reset applies to an installed entity",
            {"id": state["id"]},
        )
    runtime_slug = state["runtime_slug"]
    folder = entity_folder(type_, runtime_slug)
    if not folder or not os.path.isdir(folder):
        raise ConflictError("not_installed", "The installation folder is missing")

    package_sha = str(row.get("package_sha256") or "")
    package = store.get_cached_package(package_sha) if package_sha else None
    if not package:
        raise ConflictError(
            "package_not_cached",
            "The installed package is not in the cache; reset is unavailable offline",
            {"id": state["id"], "sha256": package_sha},
        )

    catalog_files = installer._entity_files(entity) or dict(row.get("files_json") or {})
    install_code = settings["allow_code"] if allow_code is None else bool(allow_code)
    non_code, code_files = installer._split_code_files(catalog_files)
    target_files = dict(catalog_files) if install_code else dict(non_code)
    skipped_code = [] if install_code else sorted(code_files)

    modified = sorted(detect_local_modifications(type_, runtime_slug)["files"])
    if not modified:
        return {"ok": True, "reset": False, "id": state["id"], "message": "No edits to reset"}

    install_id = ""
    with store.install_lock():
        install_id = store.new_install_id()
        staging, _backup = store.prepare_run_dirs(install_id)
        store.append_run(
            {
                "install_id": install_id,
                "type": type_,
                "id": state["id"],
                "version": state["catalog_version"],
                "action": "reset",
                "status": "applying",
            }
        )
        try:
            package_path = os.path.join(staging, "package.tar.gz")
            with open(package_path, "wb") as handle:
                handle.write(package)
            verify.verify_package(package, package_sha)
            members = verify.validate_archive(package_path)
            unpack_dir = os.path.join(staging, "content")
            verify.extract_archive(
                package_path,
                unpack_dir,
                members,
                skip=skipped_code if not install_code else [],
            )

            backup_registry = installer._backup_registry_file(install_id)
            backed_up: List[str] = []
            for rel_path in modified:
                candidate = os.path.join(folder, rel_path)
                if not os.path.isfile(candidate):
                    continue
                store.copy_into_backup(candidate, install_id, rel_path)
                backed_up.append(rel_path)
            store.write_run_meta(
                install_id,
                {
                    "install_id": install_id,
                    "type": type_,
                    "id": state["id"],
                    "runtime_slug": runtime_slug,
                    "created_folder": False,
                    "created_files": [],
                    "replaced_files": modified,
                    "removed_files": [],
                    "previous_row": row,
                    "registry_path": installer._skills_registry_path(),
                    "registry_backup": backup_registry,
                    "registry_existed": bool(backup_registry),
                    "user_backup_files": backed_up,
                },
            )

            for rel_path in modified:
                source_file = os.path.join(unpack_dir, rel_path)
                if not os.path.isfile(source_file):
                    continue
                target = os.path.join(folder, rel_path)
                os.makedirs(os.path.dirname(target) or folder, exist_ok=True)
                store._atomic_replace(source_file, target)

            files_now = registry.entity_file_manifest(type_, runtime_slug, target_files)
            registry.update_installed(
                type_,
                state["id"],
                files_json=files_now,
                tree_sha256=registry.tree_sha256(type_, runtime_slug, files_now),
                user_modified=False,
                user_modified_files_json=[],
                updated_at=store.isoformat_utc(),
                install_id=install_id,
            )
            store.update_run(
                install_id,
                status="done",
                finished_at=store.isoformat_utc(),
                replaced_files=modified,
            )
            store.cleanup_run_dirs(install_id, keep_backup=True)
            store.clear_state()
            registry.export_registry_cache()
            return {
                "ok": True,
                "reset": True,
                "id": state["id"],
                "install_id": install_id,
                "reset_files": modified,
                "backed_up_files": backed_up,
            }
        except Exception as exc:
            _fail(install_id, row, runtime_slug, str(row.get("runtime_skill_id") or ""), exc)
            if isinstance(exc, ConflictError):
                raise
            if isinstance(exc, store.MarketplaceError):
                raise ConflictError(exc.code, exc.message, exc.details) from exc
            raise ConflictError(
                "reset_failed",
                f"Could not reset the entity: {exc}",
                {"id": state["id"]},
            ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)


# ─── Removal ──────────────────────────────────────────────────────────────

def uninstall(type_: str, id_: str, *, remove_files: bool = True) -> dict:
    """Remove an installed entity and stop it from coming back.

    Three things go away together: the files, the type-level record (the
    ``skills.json`` entry) and the registry row. A marker in
    ``marketplace_removed`` is written last, so ``ensure_default_skills()``
    and the catalog both treat the entity as deliberately removed instead of
    reinstalling it on the next start.

    Removing something that is not installed is a no-op, which makes the
    button safe to press twice.
    """
    row = registry.get_installed(type_, id_)
    if row is None:
        return {"ok": True, "removed": False, "id": id_, "message": "Not installed"}

    runtime_slug = str(row.get("runtime_slug") or "")
    folder = entity_folder(type_, runtime_slug)
    removed_files = False
    # An agent owns a database row as well as a folder. Removing only the
    # folder would leave the employee or the helper listed in its own page,
    # pointing at a directory that no longer exists - so the row goes through
    # its own delete path, which also drops the folder.
    row_removed = False
    if type_ == "agent" and runtime_slug:
        kind = str(row.get("agent_kind") or "").strip() or registry.installed_agent_kind(runtime_slug)
        try:
            if kind == "assistant":
                from core.assistants import delete_assistant, get_assistant_by_slug

                existing_row = get_assistant_by_slug(runtime_slug) or {}
                if existing_row.get("id"):
                    row_removed = bool(delete_assistant(str(existing_row["id"])))
            else:
                from core.orchestrators import delete_orchestrator, get_orchestrator

                if get_orchestrator(runtime_slug) is not None:
                    row_removed = bool(delete_orchestrator(runtime_slug))
        except Exception:
            row_removed = False
        removed_files = not os.path.isdir(folder) if folder else False
    if remove_files and folder and os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
        removed_files = not os.path.isdir(folder)

    connection_removed = _remove_connection(type_, runtime_slug)

    skill_id = str(row.get("runtime_skill_id") or "")
    if type_ == "skill" and skill_id:
        try:
            from core.skills_library import delete_skill

            delete_skill(skill_id)
        except Exception:
            pass

    registry.remove_installed(type_, id_)
    registry.mark_removed(type_, id_, runtime_slug, via="user")
    registry.export_registry_cache()
    return {
        "ok": True,
        "removed": True,
        "id": id_,
        "type": type_,
        "runtime_slug": runtime_slug,
        "files_removed": removed_files,
        "row_removed": row_removed,
        "connection_removed": connection_removed,
    }


def _remove_connection(type_: str, runtime_slug: str) -> bool:
    """Remove the connection an MCP entity installed, when it is safe to.

    An MCP entity owns no folder - its installation *is* a connector
    connection, and leaving it behind would keep a server in the list that
    the user just uninstalled from the showcase. A connection the user has
    already configured is kept: it may hold credentials entered by hand and
    be used by other prompts, so removal only touches a connection still
    waiting for its credentials.

    Returns True when a connection was removed.
    """
    if type_ != "mcp" or not runtime_slug:
        return False
    try:
        from core import connectors

        manifest = connectors.get_connection(runtime_slug) or {}
        if not manifest:
            return False
        if str(manifest.get("credentials_status") or "") == "active":
            return False
        connectors.delete_connection(runtime_slug)
        return True
    except Exception:
        return False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
