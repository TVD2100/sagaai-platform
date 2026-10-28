"""
core.marketplace.agent_installer - installation of an ``agent`` entity.

An agent package ships an orchestrator folder: ``orchestrator.json``,
``system_prompt.md``, ``instructions/*.md`` and ``functions/*.py``. Installing
it means three things happen in a fixed order (D-03, D-07):

  1. the package is downloaded, hash-verified and unpacked into staging;
  2. ``config_defaults`` of the manifest are merged into the bundle config
     BEFORE the import - the importer writes the config it receives, so a
     merge after the fact would either be lost or overwrite user settings;
  3. the folder and the DB row are created by the orchestrator importer, and
     the registry row is written last as the commit marker.

The file manifest stored in the registry is hashed from the LIVE folder after
the import, not from the package: the importer regenerates
``orchestrator.json`` and ``system_prompt.md`` from the database, so the
package bytes are not what ends up on disk. The hashing area (D-04) is the
agent part of the folder - ``orchestrator.json``, ``system_prompt.md``,
``instructions/*.md`` and ``functions/*.py`` - which keeps a fresh
installation clean while a note the user drops next to it stays theirs.
"""
from __future__ import annotations

import os
import shutil
from typing import Dict, List, Optional

from core.marketplace import config as marketplace_config
from core.marketplace import registry, rollback, source as source_mod, store, verify
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


# ─── Package reading ──────────────────────────────────────────────────────

def _orchestrator_area_files(slug: str) -> Dict[str, str]:
    """Return ``{relative path: sha256}`` for the hashing area of an agent.

    The area is the agent part of the orchestrator folder: the bundle, the
    prompt, the instruction files and the custom functions (D-04). Anything
    else the user keeps in that folder - notes, exports, logs - is not part
    of the entity and never marks the installation as modified.
    """
    from core.orchestrator_folders import get_orchestrator_dir

    folder = get_orchestrator_dir(slug)
    if not os.path.isdir(folder):
        return {}
    manifest: Dict[str, str] = {}
    for root, dirs, names in os.walk(folder):
        dirs[:] = [
            name for name in dirs
            if name not in ("__pycache__", ".git")
            and os.path.relpath(os.path.join(root, name), folder).replace(os.sep, "/") in (
                "instructions", "functions"
            )
        ]
        for name in sorted(names):
            full = os.path.join(root, name)
            rel_path = os.path.relpath(full, folder).replace(os.sep, "/")
            if rel_path in ("orchestrator.json", "system_prompt.md"):
                manifest[rel_path] = registry._sha256_file(full)
            elif rel_path.startswith(("instructions/", "functions/")) and rel_path.endswith((".md", ".py")):
                manifest[rel_path] = registry._sha256_file(full)
    return manifest


def _assistant_area_files(slug: str, declared: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Return ``{relative path: sha256}`` for the hashing area of a helper.

    A helper consists of its manifest, its prompt and the attachment files
    the package ships. Attachments a user adds themselves are outside the
    area: an update must not touch them and must not report them as a
    modification of the entity.

    With *declared* the manifest of the installed version decides the area,
    which is what keeps a user-added file out of it. Without it - a legacy
    folder the registry never recorded - the folder is walked instead, which
    is the best evidence available at that point.
    """
    from core.assistant_folders import get_assistant_dir

    folder = get_assistant_dir(slug)
    if not os.path.isdir(folder):
        return {}
    if declared:
        manifest: Dict[str, str] = {}
        for rel_path in declared:
            full = os.path.join(folder, rel_path)
            manifest[rel_path] = registry._sha256_file(full) if os.path.isfile(full) else ""
        return manifest
    manifest = {}
    for name in ("manifest.json", "prompt.md"):
        full = os.path.join(folder, name)
        if os.path.isfile(full):
            manifest[name] = registry._sha256_file(full)
    files_dir = os.path.join(folder, "files")
    if os.path.isdir(files_dir):
        for name in sorted(os.listdir(files_dir)):
            full = os.path.join(files_dir, name)
            if os.path.isfile(full):
                manifest[f"files/{name}"] = registry._sha256_file(full)
    return manifest


def area_files(
    slug: str,
    agent_kind: str = "",
    declared: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Return ``{relative path: sha256}`` for the agent part of a folder.

    Public entry point for callers outside the installer (the migration and
    the update check): an agent consists of the files the platform generates
    for its kind plus what the package ships, never of the notes or exports a
    user keeps in the same folder. The kind comes from the registry when the
    caller does not know it, so a helper is not hashed as an employee.

    *declared* is the file manifest of the installed version; when given it
    decides which paths take part, which keeps a file the user added out of
    the area.
    """
    kind = str(agent_kind or "").strip() or registry.installed_agent_kind(slug)
    if not kind:
        # No registry row yet (a legacy orphan or a migration): the folder
        # that actually exists decides, so a helper is never hashed as an
        # employee.
        orchestrator_dir = os.path.join(registry.agents_root(), slug)
        kind = "orchestrator" if os.path.isdir(orchestrator_dir) else "assistant"
    if kind == "assistant":
        return _assistant_area_files(slug, declared)
    return _orchestrator_area_files(slug)


def _remove_stale_area_files(slug: str, declared: Dict[str, str], agent_kind: str = "") -> List[str]:
    """Delete area files the new version does not declare; return their paths.

    An update replaces the files of the entity, it does not add to them: an
    instruction, a function or an attachment the new version dropped has to
    leave the folder too, otherwise it stays on disk, enters the recorded
    manifest and keeps working as part of an agent that no longer ships it.
    Only the hashing area is touched - the notes a user keeps in the same
    folder and the attachments they added themselves are theirs.
    """
    kind = str(agent_kind or "").strip() or registry.installed_agent_kind(slug)
    folder = registry.agent_folder(slug, kind)
    if not folder or not os.path.isdir(folder):
        return []
    removed: List[str] = []
    for rel_path in sorted(area_files(slug, kind)):
        if rel_path in declared:
            continue
        full = os.path.join(folder, rel_path)
        if not os.path.isfile(full):
            continue
        try:
            os.remove(full)
        except OSError:
            continue
        removed.append(rel_path)
    if kind == "assistant":
        # A helper has no instruction tree to prune; its whole area is the
        # manifest, the prompt and the attachments.
        return removed
    # An empty directory left behind by a dropped instruction reads as a
    # broken entity, so the two area folders are pruned of empty subfolders.
    for area in ("instructions", "functions"):
        area_dir = os.path.join(folder, area)
        if not os.path.isdir(area_dir):
            continue
        for name in sorted(os.listdir(area_dir)):
            sub = os.path.join(area_dir, name)
            if os.path.isdir(sub) and not os.listdir(sub):
                try:
                    os.rmdir(sub)
                except OSError:
                    pass
    return removed


def _read_json(path: str) -> dict:
    """Return the parsed JSON object at *path*, or an empty dict."""
    import json

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_text(path: str) -> str:
    """Return the text at *path*, or an empty string."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _bundle_from_folder(folder: str, slug: str) -> dict:
    """Build the orchestrator import dict from an unpacked agent package.

    The returned dict has the ``sagaai_orchestrator/v1`` shape the importer
    expects: metadata and config from ``orchestrator.json``, the prompt from
    ``system_prompt.md``, one record per ``instructions/*.md`` (front matter
    stripped into id/name/description) and the raw source of every
    ``functions/*.py``.

    Raises :class:`InstallError` when the package carries no system prompt:
    an orchestrator without a prompt would be stored as an empty employee.
    """
    from core.defaults import parse_front_matter

    meta = _read_json(os.path.join(folder, "orchestrator.json"))
    if not meta:
        meta = _read_json(os.path.join(folder, "settings.json"))

    prompt = _read_text(os.path.join(folder, "system_prompt.md"))
    if not prompt.strip():
        prompt = str(meta.get("prompt_text") or "")
    if not prompt.strip():
        raise InstallError(
            "missing_prompt",
            "The agent package carries no system prompt",
            {"slug": slug},
        )

    instructions: List[dict] = []
    instr_dir = os.path.join(folder, "instructions")
    if os.path.isdir(instr_dir):
        for fname in sorted(os.listdir(instr_dir)):
            if not fname.endswith(".md"):
                continue
            raw = _read_text(os.path.join(instr_dir, fname))
            if not raw.strip():
                continue
            meta_i, body = parse_front_matter(raw, default_id=fname[:-3])
            iid = str(meta_i.get("id") or fname[:-3])
            instructions.append({
                "id": iid,
                "name": str(meta_i.get("name") or iid),
                "description": str(meta_i.get("description") or ""),
                "prompt_text": body,
            })

    functions: Dict[str, str] = {}
    func_dir = os.path.join(folder, "functions")
    if os.path.isdir(func_dir):
        for fname in sorted(os.listdir(func_dir)):
            if not fname.endswith(".py"):
                continue
            code = _read_text(os.path.join(func_dir, fname))
            if code.strip():
                functions[fname[:-3]] = code

    bundle: dict = {
        "format": "sagaai_orchestrator/v1",
        "slug": slug,
        "name": str(meta.get("name") or slug),
        "description": str(meta.get("description") or ""),
        "prompt_text": prompt,
        "config": dict(meta.get("config") or {}),
        "tools": list(meta.get("tools") or []),
        "instructions": instructions,
        "functions": functions,
    }
    # Only pass the optional fields the package actually declares: omitting
    # them lets the importer apply its own defaults instead of a zero here.
    if isinstance(meta.get("max_steps"), (int, float)):
        bundle["max_steps"] = int(meta["max_steps"])
    if isinstance(meta.get("auto_apply"), bool):
        bundle["auto_apply"] = bool(meta["auto_apply"])
    return bundle


def _assistant_bundle_from_folder(folder: str, slug: str) -> dict:
    """Build the folder payload of an assistant package.

    An assistant package holds ``manifest.json`` (metadata), ``prompt.md``
    (the system prompt) and optional ``files/*`` attachments. The returned
    dict is the export shape the assistant importer consumes, so the files
    reach the folder exactly as the package ships them.

    Raises :class:`InstallError` when the package carries no prompt: an
    assistant without one would be stored as a profile that answers nothing.
    """
    meta = _read_json(os.path.join(folder, "manifest.json"))
    if not meta:
        meta = _read_json(os.path.join(folder, "settings.json"))

    prompt = _read_text(os.path.join(folder, "prompt.md"))
    if not prompt.strip():
        prompt = str(meta.get("prompt_text") or meta.get("text") or "")
    if not prompt.strip():
        raise InstallError(
            "missing_prompt",
            "The assistant package carries no system prompt",
            {"slug": slug},
        )

    files: Dict[str, str] = {}
    files_dir = os.path.join(folder, "files")
    if os.path.isdir(files_dir):
        for fname in sorted(os.listdir(files_dir)):
            if not fname.endswith(".txt"):
                continue
            content = _read_text(os.path.join(files_dir, fname))
            if content:
                files[fname] = content

    bundle = {
        "name": str(meta.get("name") or slug),
        "description": str(meta.get("description") or ""),
        "service": str(meta.get("service") or ""),
        "model": str(meta.get("model") or ""),
        "temperature": meta.get("temperature", 0.3),
        "tools": list(meta.get("tools") or []),
        "max_tool_calls": meta.get("max_tool_calls"),
        "max_tokens": meta.get("max_tokens"),
        "reasoning_effort": meta.get("reasoning_effort"),
        "slug": slug,
    }
    return {"bundle": bundle, "prompt_text": prompt, "files": files}


def _apply_config_defaults(
    bundle: dict,
    entity: dict,
    entity_id: str,
    version: str,
    channel: str,
) -> dict:
    """Merge manifest defaults into the bundle config before the import.

    A manifest default never overwrites a key the package itself declares:
    the package ships the settings its author tested, the manifest only
    fills the gaps. The ``marketplace`` marker records which catalog entity
    the installation came from, which is what a later update and the
    removal tombstone key on.
    """
    config = dict(bundle.get("config") or {})
    defaults = entity.get("config_defaults")
    if isinstance(defaults, dict):
        for key, value in defaults.items():
            config.setdefault(str(key), value)
    config["marketplace"] = {
        "id": entity_id,
        "version": version,
        "channel": channel,
        "installed_at": store.isoformat_utc(),
    }
    bundle["config"] = config
    return bundle


# ─── Slug selection ───────────────────────────────────────────────────────

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


def _previous_package_meta(
    existing: Optional[dict],
    names: tuple = ("orchestrator.json", "settings.json"),
) -> dict:
    """Return the bundle metadata the installed package shipped.

    An update must tell a value the user changed from a value the catalog
    merely ships differently: without that baseline the only safe choice would
    be to keep everything and never update a setting. The package of the
    installed version is cached under its sha256, so the baseline is read from
    there; an absent cache answers an empty dict, which makes the merge keep
    the installed values.
    """
    package_sha = str((existing or {}).get("package_sha256") or "")
    if not package_sha:
        return {}
    try:
        package = store.get_cached_package(package_sha)
    except Exception:
        return {}
    if not package:
        return {}
    import io
    import json
    import tarfile

    try:
        with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.name not in names:
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                data = json.loads(handle.read().decode("utf-8"))
                if isinstance(data, dict):
                    return data
    except Exception:
        return {}
    return {}


def _merge_untouched(installed, package, previous):
    """Return the package value when the user never changed the installed one.

    The scalar counterpart of :func:`_merge_update_config`: a value equal to
    what the installed package shipped is a value the user left alone, so the
    update may move it. Any other value is the user's own and stays.
    """
    if installed == previous:
        return package
    return installed


def _merge_update_config(
    installed: dict,
    package: dict,
    previous: dict,
) -> dict:
    """Return the config an update writes, keeping what the user changed.

    A key the user never touched takes the value the new package ships; a key
    the user did change keeps the user's value, because an update refreshes an
    entity and never overwrites a deliberate setting.
    """
    result = dict(installed or {})
    baseline = previous if isinstance(previous, dict) else {}
    for key, value in (package or {}).items():
        if key not in result:
            result[key] = value
            continue
        if result.get(key) == baseline.get(key):
            # Untouched since the installed version: the catalog may move it.
            result[key] = value
    return result


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


# ─── Install ──────────────────────────────────────────────────────────────

def install_agent(
    entity: dict,
    *,
    channel: str,
    allow_code: Optional[bool] = None,
    source: Optional[source_mod.MarketplaceSource] = None,
    accept_license: bool = False,
    runtime_slug: Optional[str] = None,
) -> dict:
    """Install one agent of any supported kind and return a report.

    The kind decides where the folder lives and which importer turns it into
    a database row; everything that can refuse the request - the marketplace
    switch, the platform bounds, the licence, the code policy - is checked
    before the download by :func:`_install_preflight`.

    Parameters:
        entity: parsed ``entity.json`` of the agent.
        channel: release channel name recorded on the registry row.
        allow_code: install executable files; ``None`` reads the setting.
        source: catalog transport; ``None`` uses the configured catalog.
        accept_license: explicit licence acceptance for entities that
            require it.
        runtime_slug: folder name to use; ``None`` derives it from the
            manifest and appends a suffix when it is taken.

    Re-installing the same version is a no-op. Any failure after the first
    change rolls the run back, so a half-installed agent is never left
    behind.
    """
    if not isinstance(entity, dict) or not entity.get("id"):
        raise InstallError("invalid_entity", "Entity manifest has no id")
    kind = _agent_kind(entity)
    preflight = _install_preflight(
        entity,
        allow_code=allow_code,
        source=source,
        accept_license=accept_license,
    )
    noop = preflight.get("noop")
    if noop is not None:
        return noop
    if kind == "assistant":
        return _install_assistant(
            entity, preflight, channel=channel, runtime_slug=runtime_slug
        )
    return _install_orchestrator(
        entity, preflight, channel=channel, runtime_slug=runtime_slug
    )


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
        install_id = store.new_install_id()
        staging, _backup = store.prepare_run_dirs(install_id)
        slug = ""
        store.append_run(
            {
                "install_id": install_id,
                "type": "agent",
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

            slug = _resolve_slug(entity, existing, runtime_slug)
            from core.orchestrator_folders import get_orchestrator_dir

            target_folder = get_orchestrator_dir(slug)
            folder_exists = os.path.isdir(target_folder)
            # An update replaces the files of an agent that is already there:
            # every file of the hashing area is copied aside first, so a failed
            # run restores the previous version instead of deleting the agent.
            replaced_files = sorted(_orchestrator_area_files(slug)) if folder_exists else []
            for rel_path in replaced_files:
                store.copy_into_backup(
                    os.path.join(target_folder, rel_path), install_id, rel_path
                )
            # Files the new version adds did not exist before the run, so a
            # rollback has to delete them; files it replaces are restored from
            # the copies above.
            added_files = sorted(set(declared) - set(replaced_files)) if folder_exists else []
            # Metadata goes down BEFORE the first change: a rollback needs to
            # know which folder and which DB row this run created.
            store.write_run_meta(
                install_id,
                {
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
                },
            )

            bundle = _bundle_from_folder(unpack_dir, slug)
            from core.orchestrators import get_orchestrator, import_orchestrator

            if existing is not None and folder_exists:
                # An update refreshes the entity, not the user's choices: a
                # value the user changed stays, a value they never touched
                # follows the package.
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
            bundle = _apply_config_defaults(bundle, entity, entity_id, version, channel)

            imported = import_orchestrator(bundle, overwrite=True)
            if not imported.get("ok"):
                raise InstallError(
                    "import_failed",
                    str(imported.get("error") or "The orchestrator importer refused the bundle"),
                    {"id": entity_id},
                )
            created_slug = str(imported.get("slug") or "")
            if created_slug != slug:
                # A different slug means the row landed somewhere this run
                # did not plan for; recording it would hide the mismatch.
                raise InstallError(
                    "slug_mismatch",
                    f"Importer placed the agent under '{created_slug}', expected '{slug}'",
                    {"id": entity_id, "expected": slug, "actual": created_slug},
                )

            row = get_orchestrator(slug) or {}
            # An update also retires what the new version no longer ships. A
            # code file the user chose not to install is kept as it is: the
            # setting says "do not install code", not "delete my code".
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
            # The importer rewrites the bundle and the prompt, so a file the
            # package shipped may legitimately differ from the live bytes.
            # What must hold is that every area file type the package declared
            # exists on disk with a real hash.
            absent = sorted(path for path, digest in files_now.items() if not digest)
            if absent:
                raise InstallError(
                    "incomplete_install",
                    "The agent folder is missing files the manifest declares",
                    {"id": entity_id, "files": absent[:10]},
                )
            tree = registry.manifest_sha256(files_now)
            now = store.isoformat_utc()
            registry.upsert_installed(
                {
                    "type": "agent",
                    "id": entity_id,
                    "runtime_slug": slug,
                    "runtime_db_id": str(row.get("id") or "") or None,
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
                    "agent_kind": "orchestrator",
                }
            )

            store.update_run(
                install_id,
                status="done",
                finished_at=store.isoformat_utc(),
                runtime_slug=slug,
            )
            store.cleanup_run_dirs(install_id, keep_backup=True)
            store.clear_state()
            try:
                registry.unmark_removed("agent", entity_id)
            except Exception:
                pass
            registry.export_registry_cache()

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
            _fail_run(install_id, slug, exc, agent_kind="orchestrator")
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


# ─── Assistant install ────────────────────────────────────────────────────

def _previous_package_text(existing: Optional[dict], name: str) -> str:
    """Return one text member of the installed package, or '' when absent.

    The baseline for "did the user edit this" lives in the cached package of
    the installed version; an absent cache answers an empty string, which
    makes the caller keep the installed text.
    """
    package_sha = str((existing or {}).get("package_sha256") or "")
    if not package_sha:
        return ""
    try:
        package = store.get_cached_package(package_sha)
    except Exception:
        return ""
    if not package:
        return ""
    import io
    import tarfile

    try:
        with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.name != name:
                    continue
                handle = archive.extractfile(member)
                if handle is not None:
                    return handle.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    return ""


def _merge_assistant_payload(
    payload: dict,
    installed_row: dict,
    previous: dict,
    existing: Optional[dict],
) -> dict:
    """Return the helper payload an update writes, keeping user choices.

    A field the user never changed follows the new package, a field they did
    change stays. The baseline is the manifest of the installed version taken
    from the package cache; without it every installed value counts as the
    user's own, which is the safe direction - an update then changes nothing
    rather than overwriting a deliberate setting.
    """
    bundle = dict(payload.get("bundle") or {})
    for key in (
        "name", "description", "service", "model", "temperature",
        "max_tool_calls", "max_tokens", "reasoning_effort",
    ):
        bundle[key] = _merge_untouched(
            installed_row.get(key), bundle.get(key), previous.get(key)
        )
    bundle["tools"] = _merge_untouched(
        list(installed_row.get("tools") or []),
        list(bundle.get("tools") or []),
        list(previous.get("tools") or []),
    )
    previous_prompt = _previous_package_text(existing, "prompt.md")
    installed_prompt = str(installed_row.get("text") or "")
    if installed_prompt.strip() and installed_prompt != previous_prompt:
        payload["prompt_text"] = installed_prompt
    payload["bundle"] = bundle
    return payload


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

    # The storage layer keeps attachments as ``.txt``; a package that ships
    # anything else is refused here rather than landing half-installed.
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

    with store.install_lock():
        install_id = store.new_install_id()
        staging, _backup = store.prepare_run_dirs(install_id)
        slug = ""
        store.append_run(
            {
                "install_id": install_id,
                "type": "agent",
                "id": entity_id,
                "version": version,
                "action": "install",
                "agent_kind": "assistant",
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

            slug = _resolve_slug(entity, existing, runtime_slug, "assistant")
            from core.assistant_folders import (
                get_assistant_dir,
                list_assistant_files,
                load_assistant_file_content,
                import_assistant_folder,
                save_assistant_file,
            )

            folder = get_assistant_dir(slug)
            folder_exists = os.path.isdir(folder)
            recorded = dict((existing or {}).get("files_json") or {})
            replaced_files = sorted(_assistant_area_files(slug, recorded)) if folder_exists else []
            package_attachments = {
                path.split("/", 1)[1] for path in declared if path.startswith("files/")
            }
            # Attachments the user added are copied into the run backup and
            # put back after the import; the backup also restores them when
            # the run fails.
            preserved: Dict[str, str] = {}
            if folder_exists:
                for fname in list_assistant_files(slug):
                    if fname in package_attachments:
                        continue
                    preserved[f"files/{fname}"] = load_assistant_file_content(slug, fname)
            for rel_path in sorted(set(replaced_files) | set(preserved)):
                store.copy_into_backup(os.path.join(folder, rel_path), install_id, rel_path)
            added_files = sorted(set(declared) - set(replaced_files)) if folder_exists else []
            store.write_run_meta(
                install_id,
                {
                    "install_id": install_id,
                    "type": "agent",
                    "agent_kind": "assistant",
                    "id": entity_id,
                    "runtime_slug": slug,
                    "created_folder": not folder_exists,
                    "created_files": added_files,
                    "replaced_files": sorted(set(replaced_files) | set(preserved)),
                    "removed_files": [],
                    "previous_row": existing,
                    "registry_path": "",
                    "registry_backup": "",
                    "registry_existed": False,
                    "created_db_row": existing is None,
                },
            )

            payload = _assistant_bundle_from_folder(unpack_dir, slug)
            from core.assistants import get_assistant_by_id, get_assistant_by_slug
            from core.assistants import reload_assistant_from_folder

            if existing is not None and folder_exists:
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
            # The marker records which catalog entity the profile came from,
            # which is what a later update and the removal marker key on.
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
            assistant_id = str(reloaded.get("id") or "")

            # An update retires what the new version no longer ships. Only the
            # files the previous version recorded are candidates: a file the
            # user added is theirs.
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
            tree = registry.manifest_sha256(files_now)
            now = store.isoformat_utc()
            registry.upsert_installed(
                {
                    "type": "agent",
                    "id": entity_id,
                    "runtime_slug": slug,
                    "runtime_db_id": assistant_id or None,
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
                    "agent_kind": "assistant",
                }
            )

            store.update_run(
                install_id,
                status="done",
                finished_at=store.isoformat_utc(),
                runtime_slug=slug,
                assistant_id=assistant_id,
            )
            store.cleanup_run_dirs(install_id, keep_backup=True)
            store.clear_state()
            try:
                registry.unmark_removed("agent", entity_id)
            except Exception:
                pass
            registry.export_registry_cache()

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
            _fail_run(install_id, slug, exc, agent_kind="assistant")
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


def _fail_run(install_id: str, slug: str, exc: Exception, agent_kind: str = "") -> None:
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
