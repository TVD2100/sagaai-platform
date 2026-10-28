# -*- coding: utf-8 -*-
"""
core.default_imports - import bundled defaults/ entities on first boot.

This module contains the high-level import functions that read the defaults/
layout (see core.defaults) and create the corresponding runtime entities:

  * default orchestrators  - defaults/orchestrators/* (except dev_agent)
  * default assistants     - defaults/assistants/*
  * default instructions   - defaults/orchestrators/dev_agent/instructions/*.md
  * default skills         - defaults/skills/*

The functions are idempotent: existing entities are never overwritten.
Deleting a file/folder under defaults/ excludes the entity from import.

Default skills behave slightly differently from the other entities: they are
imported not only on a fresh library but also into a non-empty one (each
preset once), so newly bundled skills appear on existing installs. User
intent is preserved via registry ``source`` markers and a removed-defaults
list (see raw string details inside ``ensure_default_skills``).

A skill that lives in the catalog as well is seeded from the bundle and
keeps its preset name as its folder, so the catalog recognises it instead of
offering a second copy; a skill the catalog already installed is never
seeded again.

After the imports, a folders-first sync (core.entity_sync) runs so every
assistant / orchestrator folder is reflected in the DB cache and every legacy
DB record gets a folder on disk.
"""
from __future__ import annotations

import json
import os
from typing import Dict

from core import defaults as defaults_mod
from core.fs import ensure_dir
from storage.models import DEFAULT_MAX_STEPS
from core.orchestrators import (
    DEVAGENT_SLUG,
    DEFAULT_WEB_SEARCH_PROMPT,
    get_orchestrator,
    import_orchestrator,
    save_orchestrator,
)


def _full_devagent_toolset() -> list:
    """Return the full DevAgent tool name list (core + workspace tools)."""
    try:
        from dev_agent.tool_executor import TOOL_CATALOG as CORE_TOOLS
        from dev_agent.universal_agent import WORKSPACE_TOOL_CATALOG
        return [t["name"] for t in CORE_TOOLS] + [t["name"] for t in WORKSPACE_TOOL_CATALOG]
    except Exception:
        return []


#: Orchestrators that moved from the bundled defaults into the marketplace
#: catalog. Their folders stay under defaults/ so a platform rollback never
#: loses them, but a fresh installation no longer seeds them: they arrive
#: from the catalog, and an existing installation keeps its runtime copy.
CATALOG_ONLY_ORCHESTRATORS = frozenset({"ya_agent"})

#: RAG bases that moved into the catalog (same rule as the orchestrators).
CATALOG_ONLY_RAG_BASES = frozenset({"yaagentai_2020"})

#: Skills that moved into the catalog. The bundled folder stays in the
#: distribution: an installation without the catalog keeps seeding it, and a
#: platform rollback finds it exactly where it was.
#:
#: Empty today: every bundled skill is seeded again on a fresh installation,
#: and the catalog recognises the seeded copy as an existing installation
#: instead of offering a second one. The hook stays in place for a skill whose
#: bundled copy must not be seeded at all.
CATALOG_ONLY_SKILLS: frozenset = frozenset()

#: Prefix of the ``source`` marker the marketplace writes into the record of
#: a skill it installed (``marketplace/<catalog id>``).
CATALOG_SOURCE_PREFIX = "marketplace/"


def _marketplace_active() -> bool:
    """True when the marketplace section is switched on.

    The master switch governs the bundled defaults too: with the section
    switched off the platform must behave exactly as it did before the
    catalog existed, catalog-only entities included.
    """
    try:
        from core.marketplace import is_enabled as marketplace_enabled

        return bool(marketplace_enabled())
    except Exception:
        return False


def _marketplace_removed_slugs(type_: str) -> set:
    """Return the runtime slugs the marketplace recorded as removed.

    A catalog entity the user deleted must not be recreated by the bundled
    default import: the two removal registries (the marketplace one and the
    pre-marketplace marker files) answer the same question, and this bridges
    them for the import paths that predate the catalog.

    A disabled marketplace contributes nothing - with the section switched
    off the bundled defaults keep working exactly as they did before.
    """
    try:
        from core.marketplace import is_enabled as marketplace_enabled

        if not marketplace_enabled():
            return set()
        from core.marketplace import registry

        return {
            str(row.get("runtime_slug") or "")
            for row in registry.list_removed(type_)
            if str(row.get("runtime_slug") or "").strip()
        }
    except Exception:
        return set()


def ensure_default_orchestrators() -> Dict[str, str]:
    """Import default orchestrators from defaults/orchestrators/*.

    The built-in dev_agent orchestrator is handled separately by
    ``core.orchestrators.ensure_builtin_orchestrators`` and is skipped here.
    The built-in DevAgent system prompt lives ONLY in
    dev_agent/system_prompt.md (single source of truth); defaults/ and legacy
    presets/ do not duplicate it.

    When defaults/orchestrators/ is missing or contains no folders, nothing
    is imported - there is no presets fallback anymore.

    An empty ``tools`` list in the default means "the full DevAgent tool set".
    The web-search prompt default is backfilled when missing.

    Returns a status dict: {slug: "created"|"exists"|"error: ..."}.
    """
    results: Dict[str, str] = {}
    # No presets fallback: defaults/orchestrators/ is the only source for
    # non-built-in default orchestrators. A missing folder simply yields an
    # empty result.
    if not os.path.isdir(defaults_mod.orchestrators_dir()):
        return results

    slugs = defaults_mod.list_default_orchestrator_slugs()
    non_dev = [s for s in slugs if s != DEVAGENT_SLUG]
    # When defaults/ exists, ONLY its folders are imported. Deleting a folder
    # silently excludes that entity (no presets fallback).

    removed_slugs = _marketplace_removed_slugs("agent")

    for slug in non_dev:
        if slug in removed_slugs:
            # The user deleted this entity. Recreating its folder here would
            # resurrect exactly what the registry recorded as removed; the
            # status says "exists" because nothing was created.
            results[slug] = "exists"
            continue
        if slug in CATALOG_ONLY_ORCHESTRATORS and _marketplace_active():
            # A catalog entity: seeding it here would bypass the registry and
            # make a fresh installation look like a catalog install. With the
            # section switched off the bundled folder stays the only source.
            continue
        data = defaults_mod.load_default_orchestrator(slug)
        if data is None:
            results[slug] = "error: unreadable"
            continue
        current = get_orchestrator(slug)
        cfg = data.get("config")
        if not isinstance(cfg, dict):
            cfg = {}
        cfg.setdefault("web_search_prompt", DEFAULT_WEB_SEARCH_PROMPT)
        # Backfill the RAG-bases assignment: preset rag_bases are merged via
        # union so both new and already existing orchestrators get the
        # bundled bases checked, while user settings stay untouched.
        preset_rag_bases = cfg.get("rag_bases") or []
        if current:
            # One-time migration of the legacy 100-step default: existing
            # default orchestrators created with the old default are upgraded
            # to the current default once, guarded by a config marker so a
            # user-chosen max_steps is never overwritten.
            try:
                current_cfg = dict(current.get("config", {}) or {})
                if (not current_cfg.get("max_steps_default_migrated")
                        and current.get("max_steps") == 100):
                    current_cfg["max_steps_default_migrated"] = True
                    save_orchestrator(slug, config=current_cfg,
                                      max_steps=DEFAULT_MAX_STEPS)
            except Exception:
                pass
            # Backfill the preset base assignment only when the user has never
            # chosen a rag_bases list (legacy config, key missing). An explicit
            # user choice - including an empty list - is never overwritten.
            if preset_rag_bases:
                try:
                    current_cfg = dict(current.get("config", {}) or {})
                    if "rag_bases" not in current_cfg:
                        current_cfg["rag_bases"] = _merge_preset_rag_bases(
                            [], preset_rag_bases
                        )
                        save_orchestrator(slug, config=current_cfg)
                except Exception:
                    pass
            results[slug] = "exists"
            continue
        # Empty tools list -> full DevAgent tool set (legacy-preset semantics).
        if not data.get("tools"):
            data["tools"] = _full_devagent_toolset()
        data["config"] = cfg
        try:
            res = import_orchestrator(data, overwrite=False)
            if res.get("ok"):
                sort_order = int(data.get("sort_order") or 150)
                save_orchestrator(slug, sort_order=sort_order)
                results[slug] = "created"
            else:
                results[slug] = "error: " + str(res.get("error", "unknown"))
        except Exception as exc:
            results[slug] = "error: " + str(exc)
    return results


def ensure_default_rag_bases() -> Dict[str, str]:
    """Seed runtime RAG bases from defaults/rag_bases/*.

    Each preset folder (manifest.json + index.db) is copied into
    DATA_DIR/rag_bases/<slug>/ when a base with that slug does not exist
    yet. The runtime copy gets a ``source: defaults/rag_bases/<slug>``
    marker in its manifest; bases the user deleted are never resurrected
    (their markers live in DATA_DIR/rag_bases/.defaults_removed.json).

    Returns a status dict: {slug: "created"|"exists"|"error: ..."}.
    """
    import shutil
    from core import rag

    results: Dict[str, str] = {}
    src_root = defaults_mod.rag_bases_dir()
    if not os.path.isdir(src_root):
        return results

    ensure_dir(rag.RAG_BASES_DIR)
    removed = set(rag._load_removed_defaults())
    removed_slugs = _marketplace_removed_slugs("rag")

    for slug in defaults_mod.list_default_rag_base_slugs():
        source_marker = f"defaults/rag_bases/{slug}"
        if source_marker in removed:
            results[slug] = "exists"
            continue
        if slug in removed_slugs:
            # A base deleted through the marketplace must not be copied back
            # from the bundled defaults: the registry recorded the intent.
            results[slug] = "exists"
            continue
        if slug in CATALOG_ONLY_RAG_BASES and _marketplace_active():
            # A catalog base: the marketplace installs it, and seeding it here
            # would hide the catalog install behind a bundled copy. With the
            # section switched off the bundled folder stays the only source.
            continue
        dst_folder = os.path.join(rag.RAG_BASES_DIR, slug)
        if os.path.isdir(dst_folder):
            # Preset-backed bases get the source marker retroactively so a
            # later manual delete is honoured (never resurrected).
            _stamp_manifest_source(dst_folder, source_marker)
            results[slug] = "exists"
            continue
        data = defaults_mod.load_default_rag_base(slug)
        if data is None:
            results[slug] = "error: unreadable"
            continue
        src_folder = os.path.join(src_root, slug)
        try:
            shutil.copytree(src_folder, dst_folder)
        except Exception as exc:
            results[slug] = "error: " + str(exc)
            continue
        # Stamp the runtime copy with its preset source.
        _stamp_manifest_source(dst_folder, source_marker)
        results[slug] = "created"
    return results


def _stamp_manifest_source(folder: str, source_marker: str) -> None:
    """Add the ``source`` preset marker to a base manifest (idempotent).

    When the manifest already carries a ``source`` value, it is not
    overwritten. Only the added field is written; all other manifest values
    stay untouched.
    """
    manifest_path = os.path.join(folder, "manifest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception:
        return
    if not isinstance(manifest, dict):
        return
    if manifest.get("source") == source_marker:
        return
    if manifest.get("source"):
        # A base with a different source marker is user-owned; skip.
        return
    manifest["source"] = source_marker
    try:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _merge_preset_rag_bases(current: list, preset: list) -> list:
    """Union of two rag_bases lists (slugs are normalized)."""
    out = []
    for item in list(current or []) + list(preset or []):
        if isinstance(item, str) and item.strip():
            val = item.strip().lower()
            if val not in out:
                out.append(val)
    return out


def ensure_default_assistants() -> Dict[str, str]:
    """Import default assistants from defaults/assistants/*.

    An assistant is created only when no assistant with the same name exists.
    Attachment files from the assistant's files/ folder are saved alongside.
    Creation goes through ``core.assistants.create_assistant`` which also
    creates the assistant's folder (DATA_DIR/assistants/<slug>/).

    Returns a status dict: {name: "created"|"exists"|"error: ..."}.
    """
    from core.assistants import (
        load_assistants_index,
        create_assistant,
        save_assistant_file,
    )

    results: Dict[str, str] = {}
    folders = defaults_mod.list_default_assistant_folders()
    if not folders:
        return results

    existing = {a.get("name", ""): a for a in load_assistants_index()}
    for folder in folders:
        data = defaults_mod.load_default_assistant(folder)
        if data is None:
            results[folder] = "error: unreadable"
            continue
        name = data.get("name") or folder
        if name in existing:
            results[name] = "exists"
            continue
        try:
            pid = create_assistant(
                name=name,
                service=data.get("service", "") or "",
                model=data.get("model", "") or "",
                temperature=data.get("temperature", 0.3) or 0.3,
                text=data.get("prompt_text", "") or "",
                description=data.get("description", "") or "",
                tools=data.get("tools") or [],
                max_tool_calls=data.get("max_tool_calls"),
                max_tokens=data.get("max_tokens"),
                reasoning_effort=data.get("reasoning_effort"),
            )
            if not pid:
                results[name] = "error: create failed"
                continue
            # Persist the assistant's RAG-base bindings into its folder
            # manifest (function-calling assistants search these bases).
            from core.assistant_folders import (
                set_assistant_rag_bases,
                set_assistant_web_search_settings,
            )
            from storage.repository import repo_get_assistant_with_text
            _full = repo_get_assistant_with_text(pid)
            _slug = (_full or {}).get("slug") or ""
            _bases = data.get("rag_bases") or []
            if _slug and _bases:
                set_assistant_rag_bases(_slug, _bases)
            # Persist per-assistant web-search overrides (context size and
            # allowed domains declared in the preset manifest).
            if _slug:
                set_assistant_web_search_settings(
                    _slug,
                    context_size=data.get("web_search_context_size"),
                    allowed_domains=data.get("web_search_allowed_domains") or None,
                )
            for fname, content in (data.get("files") or {}).items():
                base = fname
                if "." in fname:
                    base = fname.rsplit(".", 1)[0]
                save_assistant_file(pid, base, content)
            results[name] = "created"
        except Exception as exc:
            results[name] = "error: " + str(exc)
    return results


def ensure_default_instructions() -> Dict[str, str]:
    """Import default instructions from defaults/orchestrators/dev_agent/instructions/.

    Each .md file with front-matter (id/name/description) becomes an
    orchestrator-specific instruction of the built-in DevAgent. Existing
    instructions are never overwritten. When defaults/ is missing, falls back
    to the legacy bootstrap (hardcoded built-in strings).

    Returns the status dict of ``core.bootstrap.ensure_instructions`` or a
    similar dict {"devagent_instructions": {id: status}}.
    """
    from core.orchestrators import (
        orch_get_instruction,
        orch_save_instruction,
        orch_delete_instruction,
    )

    instr_dir = os.path.join(defaults_mod.orchestrators_dir(), DEVAGENT_SLUG, "instructions")
    if not os.path.isdir(instr_dir):
        from core.bootstrap import ensure_instructions
        return ensure_instructions()

    devagent_status: Dict[str, str] = {}
    for fname in sorted(os.listdir(instr_dir)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(instr_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            continue
        default_id = fname[:-3]
        meta, body = defaults_mod.parse_front_matter(raw, default_id=default_id)
        iid = meta.get("id") or default_id
        name = meta.get("name") or iid
        description = meta.get("description", "")
        existing = orch_get_instruction(DEVAGENT_SLUG, iid)
        if iid == "employee_creator":
            # Migrate the legacy orchestrator_creator id: its saved text
            # (possibly user-edited) becomes the canonical employee_creator
            # instruction when the canonical one is missing; the legacy row is
            # always removed.
            legacy_orch = orch_get_instruction(DEVAGENT_SLUG, "orchestrator_creator")
            if legacy_orch:
                if existing is None:
                    name = legacy_orch.get("name", "") or name
                    description = legacy_orch.get("description", "") or description
                    body = legacy_orch.get("text", "") or body
                orch_delete_instruction(DEVAGENT_SLUG, "orchestrator_creator")
        if existing:
            devagent_status[iid] = "exists"
            continue

        ok = orch_save_instruction(
            DEVAGENT_SLUG, iid,
            name=name,
            description=description,
            prompt_text=body,
        )
        devagent_status[iid] = "created" if ok else "error"
    return {"devagent_instructions": devagent_status}


def ensure_default_skills() -> Dict[str, str]:
    """Seed the skills library from defaults/skills/*.

    Each subfolder of defaults/skills/ that contains files is imported as a
    skill when it is not already present in the runtime library. Works on
    installations with a non-empty library too (original empty-library-only
    behaviour is gone): every defaults preset gets a
    ``source: defaults/<folder>`` registry marker and is skipped when that
    marker already exists.

    Skills the user deleted are never resurrected: deleting a preset-backed
    skill records its marker in the library's removed-defaults list, which
    is honoured here.

    A folder the marketplace already filled with the same skill is not seeded
    twice: the catalog calls a bundled copy an existing installation
    (``adopt``) rather than a free slot, so a second copy under a suffixed
    name would show one installation as two entries.

    Newly imported skills are enabled for the built-in DevAgent
    orchestrator (config.enabled_skills).

    Returns a status dict: {folder: skill_id | "exists" | "error: ..."}.
    """
    from core.skills_library import (
        _load_registry,
        _load_removed_defaults,
        _save_registry,
        get_skills_root,
        import_skill_from_folder,
        PLATFORM_DEVELOPER,
    )

    results: Dict[str, str] = {}
    src_root = defaults_mod.skills_dir()
    if not os.path.isdir(src_root):
        return results

    removed = set(_load_removed_defaults())
    # Removal markers written by the marketplace are a second source: a skill
    # deleted from the catalog UI must never be resurrected by this import
    # either, and its marker lives in the marketplace_removed table rather
    # than in removed_defaults.json.
    try:
        from core.marketplace.registry import removed_source_markers

        removed.update(removed_source_markers())
    except Exception:
        pass
    registry = _load_registry()
    known_markers = {
        str(r.get("source") or "")
        for r in registry.values()
        if isinstance(r, dict)
    }
    # A skill the catalog installed occupies the folder a bundled preset would
    # take. Its record is the evidence that the folder is already in use:
    # seeding the preset next to it would produce a second copy under a
    # suffixed name and one installation would show up twice.
    catalog_folders = {
        str(r.get("folder") or "")
        for r in registry.values()
        if isinstance(r, dict)
        and str(r.get("source") or "").startswith(CATALOG_SOURCE_PREFIX)
    }

    for name in sorted(os.listdir(src_root)):
        src = os.path.join(src_root, name)
        if name.startswith(".") or not os.path.isdir(src):
            continue
        has_files = any(
            os.path.isfile(os.path.join(r, f))
            for r, _dirs, files in os.walk(src)
            for f in files
        )
        if not has_files:
            continue
        source_marker = f"defaults/{name}"
        if source_marker in removed or source_marker in known_markers:
            results[name] = "exists"
            continue
        if f"{CATALOG_SOURCE_PREFIX}{name}" in known_markers:
            results[name] = "exists"
            continue
        if name in catalog_folders:
            results[name] = "exists"
            continue
        if name in CATALOG_ONLY_SKILLS and _marketplace_active():
            # A catalog skill: the marketplace installs it, and seeding it here
            # would hide the catalog install behind a bundled copy. With the
            # section switched off the bundled folder stays the only source.
            continue
        # The bundled preset keeps its folder name: the catalog addresses the
        # same skill by that name, so an adopted installation and a catalog
        # installation land in one place. A folder that already exists without
        # a record is a stranger - the library then picks a free name.
        folder_arg = name if not os.path.isdir(os.path.join(get_skills_root(), name)) else None
        try:
            res = import_skill_from_folder(
                src, name=name.replace("_", " ").title(),
                developer=PLATFORM_DEVELOPER, adapted=True, folder=folder_arg
            )
        except Exception as exc:
            results[name] = "error: " + str(exc)
            continue
        skill_id = res.get("skill", {}).get("id") if res.get("ok") else ""
        if not skill_id:
            results[name] = "error"
            continue
        registry = _load_registry()
        rec = registry.get(skill_id)
        if rec is not None:
            rec["source"] = source_marker
            _save_registry(registry)
        results[name] = skill_id

    # Enable newly imported skills for DevAgent.
    new_ids = [
        v for v in results.values()
        if v and not str(v).startswith("error") and v != "exists"
    ]
    if new_ids:
        try:
            from core.orchestrators import (
                get_enabled_skills,
                set_enabled_skills,
            )
            current = set(get_enabled_skills(DEVAGENT_SLUG))
            current.update(new_ids)
            set_enabled_skills(DEVAGENT_SLUG, sorted(current))
        except Exception:
            pass

    return results


def ensure_all_defaults() -> Dict[str, Dict[str, str]]:
    """Run every default-import step in the correct order.

    Creates the built-in DevAgent (and default orchestrators), default
    assistants, default instructions and seeds default skills. Finally runs
    the folders-first sync so every assistant/orchestrator folder is in sync
    with the DB cache (idempotent).

    Returns {"orchestrators": {...}, "assistants": {...},
             "instructions": {...}, "skills": {...}, "folders": {...}}.
    """
    from core.orchestrators import ensure_builtin_orchestrators
    from core.instructions import ensure_global_instructions
    results: Dict[str, Dict[str, str]] = {
        "rag_bases": ensure_default_rag_bases(),
        "orchestrators": ensure_builtin_orchestrators(),
        "assistants": ensure_default_assistants(),
        "instructions": ensure_default_instructions(),
        "global_instructions": ensure_global_instructions(),
        "skills": ensure_default_skills(),
    }
    try:
        from core.entity_sync import ensure_entity_folders_sync
        results["folders"] = ensure_entity_folders_sync()
    except Exception as exc:
        results["folders"] = {"_error": str(exc)}
    return results
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
