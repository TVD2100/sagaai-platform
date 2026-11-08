"""
core.marketplace.agent_run - the run ledger of an agent installation.

An agent install is a run: it opens a numbered run, writes the backup
metadata before the first change, records the result in the registry and
closes the run. A failure rolls the run back and records why.

The one ordering rule this module exists to keep: :func:`write_agent_run_meta`
lands BEFORE the first change of the live folder and :func:`register_agent`
lands LAST - the registry row is the commit marker of the whole run.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from core.marketplace import registry, rollback, store
from core.marketplace.package_pin import previous_package_sha, refresh_package_pin


def open_agent_run(
    entity_id: str,
    version: str,
    *,
    agent_kind: str,
) -> tuple:
    """Open a run and return ``(install_id, staging, backup)``.

    The run is recorded as ``applying`` before the first network call: a
    process that dies mid-install leaves a record a later recovery can roll
    back, and a run that was never recorded would leave files nobody owns.
    """
    install_id = store.new_install_id()
    staging, backup = store.prepare_run_dirs(install_id)
    record = {
        "install_id": install_id,
        "type": "agent",
        "id": entity_id,
        "version": version,
        "action": "install",
        "status": "applying",
    }
    if agent_kind:
        record["agent_kind"] = agent_kind
    store.append_run(record)
    return install_id, staging, backup


def write_agent_run_meta(
    install_id: str,
    *,
    entity_id: str,
    slug: str,
    folder_exists: bool,
    existing: Optional[dict],
    added_files: List[str],
    replaced_files: List[str],
    agent_kind: str = "",
) -> None:
    """Record what a rollback needs before the first change of the folder.

    The metadata names the folder and the database row this run creates or
    replaces, so a rollback can undo exactly this run and nothing else.
    """
    meta = {
        "install_id": install_id,
        "type": "agent",
        "id": entity_id,
        "runtime_slug": slug,
        "created_folder": not folder_exists,
        "created_files": added_files,
        "replaced_files": replaced_files,
        "removed_files": [],
        "previous_row": existing,
        "registry_path": "",
        "registry_backup": "",
        "registry_existed": False,
        "created_db_row": existing is None,
    }
    if agent_kind:
        meta["agent_kind"] = agent_kind
    store.write_run_meta(install_id, meta)


def register_agent(*, entity: dict, entity_id: str, slug: str, version: str, channel: str,
                   package_sha: str, files_now: Dict[str, str], skipped_code: List[str],
                   install_id: str, license_at: Optional[str], agent_kind: str,
                   runtime_db_id: Optional[str]) -> str:
    """Write the registry row of an agent and return its tree hash.

    This call is the commit marker of the run: everything before it can be
    rolled back, everything after it is bookkeeping. The row records the
    files actually on disk, not the bytes the package shipped - the importer
    regenerates the bundle and the prompt from the database.
    """
    tree = registry.manifest_sha256(files_now)
    now = store.isoformat_utc()
    previous_sha = previous_package_sha(entity_id)
    registry.upsert_installed(
        {
            "type": "agent",
            "id": entity_id,
            "runtime_slug": slug,
            "runtime_db_id": runtime_db_id or None,
            "runtime_skill_id": None,
            "fork_of": entity.get("fork_of"),
            "version": version,
            "channel": channel,
            "source_url": str(entity.get("source_url") or ""),
            "package_sha256": package_sha,
            "tree_sha256": tree,
            "index_sha256": None,
            "files_json": files_now,
            "skipped_code_files_json": skipped_code,
            "installed_at": now,
            "updated_at": now,
            "installed_by": "marketplace",
            "user_modified": False,
            "user_modified_files_json": [],
            "install_id": install_id,
            "bundle_id": entity.get("bundle_id"),
            "pinned": False,
            "license_accepted_at": license_at,
            "agent_kind": agent_kind,
        }
    )
    refresh_package_pin(package_sha, previous_sha)
    return tree


def commit_agent_run(
    install_id: str,
    *,
    slug: str,
    entity_id: str,
    agent_kind: str,
    extra: Optional[dict] = None,
) -> None:
    """Close a successful run: mark it done and clear the live state.

    The backup directory is kept on purpose - it is what a later "bring my
    version back" action and the run history read.
    """
    fields = {
        "status": "done",
        "finished_at": store.isoformat_utc(),
        "runtime_slug": slug,
    }
    if extra:
        fields.update(extra)
    store.update_run(install_id, **fields)
    store.cleanup_run_dirs(install_id, keep_backup=True)
    store.clear_state()
    try:
        registry.unmark_removed("agent", entity_id)
    except Exception:
        pass
    registry.export_registry_cache()


def fail_agent_run(install_id: str, slug: str, exc: Exception, agent_kind: str = "") -> None:
    """Roll a failed agent run back and record the failure.

    Rollback errors must not mask the original exception: they land in the
    run log, while the caller still sees the real cause. When the run left no
    usable backup metadata, the entity is removed directly - an employee
    through its own delete path, a helper through the assistant one, so a
    failed attempt leaves nothing half-alive behind.
    """
    rolled_back = False
    try:
        if store.read_run_meta(install_id) is not None:
            rollback.rollback_run(install_id, reason=f"agent install failed: {exc}")
            rolled_back = True
    except Exception:
        rolled_back = False
    if not rolled_back and slug:
        kind = str(agent_kind or "").strip() or registry.installed_agent_kind(slug)
        try:
            if kind == "assistant":
                from core.assistant_folders import remove_assistant_dir
                from core.assistants import delete_assistant, get_assistant_by_slug

                row = get_assistant_by_slug(slug) or {}
                if row.get("id"):
                    delete_assistant(str(row["id"]))
                else:
                    remove_assistant_dir(slug)
            else:
                from core.orchestrators import delete_orchestrator

                delete_orchestrator(slug)
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


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
