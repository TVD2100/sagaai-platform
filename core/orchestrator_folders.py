# -*- coding: utf-8 -*-
"""
core.orchestrator_folders - file-system storage for orchestrator assets.

Each orchestrator has its own folder under DATA_DIR/orchestrators/<slug>/:

    orchestrator.json          - full orchestrator export (prompt, config, tools,
                                 instructions, functions list)
    system_prompt.md           - the latest system prompt text
    instructions/<id>.md       - orchestrator-specific instructions, one file
                                 per instruction with front-matter
    functions/<name>.py        - custom Python functions the orchestrator can call

Keeping everything in a folder makes export/import trivial: the folder can be
zipped or copied. DevAgent can read/edit these files directly, which allows
iteration on orchestrator behaviour through the normal DevAgent workflow.

The folder is the *source of truth*. A dedicated DB table
(orchestrator_instructions) caches instructions so the hot path
(extending the orchestrator prompt with available instructions) never reads
from disk. The cache is rebuilt at startup and is refreshed after every
write through this API.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import core.paths
from core.fs import ensure_dir, read_json_file, read_text_file, write_text_file
from storage.repository import (
    repo_list_orchestrator_instructions,
    repo_get_orchestrator_instruction,
    repo_save_orchestrator_instruction,
    repo_delete_orchestrator_instruction,
    repo_delete_all_orchestrator_instructions,
)


# ─── Folder layout helpers ────────────────────────────────────────────────────

def get_orchestrators_root() -> str:
    """Return the root directory where all orchestrator folders live."""
    return os.path.join(core.paths.DATA_DIR, "orchestrators")


def safe_orchestrator_slug(slug: str) -> str:
    """Normalize an arbitrary slug to a safe folder/DB identifier.

    The result contains only lowercase ascii letters, digits and
    underscores. Every other character (spaces, dashes, dots, slashes,
    cyrillic, ...) is replaced with an underscore; runs of underscores are
    collapsed and leading/trailing underscores are stripped. Returns '' for
    empty input so callers can reject it before touching the filesystem.
    """
    raw = (slug or "").strip().lower()
    safe = re.sub(r"[^a-z0-9_]+", "_", raw)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe


def get_orchestrator_dir(slug: str) -> str:
    """Return the folder path for a single orchestrator (not necessarily existing).

    The slug is normalized with safe_orchestrator_slug first, so the
    returned path can never escape the orchestrators root (no dots, no
    slashes). Falls back to 'unnamed' when the normalization yields an
    empty string.
    """
    safe = safe_orchestrator_slug(slug)
    if not safe:
        safe = "unnamed"
    return os.path.join(get_orchestrators_root(), safe)


def ensure_orchestrator_dir(slug: str) -> str:
    """Create and return the orchestrator folder path."""
    d = get_orchestrator_dir(slug)
    ensure_dir(d)
    ensure_dir(os.path.join(d, "functions"))
    ensure_dir(os.path.join(d, "instructions"))
    return d


def remove_orchestrator_dir(slug: str) -> bool:
    """Recursively delete the orchestrator folder. Returns True on success."""
    import shutil
    d = get_orchestrator_dir(slug)
    try:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        return True
    except Exception:
        return False


def orchestrator_folder_exists(slug: str) -> bool:
    """True if the orchestrator already has a personal folder on disk."""
    return os.path.isdir(get_orchestrator_dir(slug))


def list_orchestrator_folder_slugs() -> List[str]:
    """Return sorted folder names of all orchestrator folders on disk."""
    root = get_orchestrators_root()
    if not os.path.isdir(root):
        return []
    return sorted(
        n for n in os.listdir(root)
        if not n.startswith(".") and os.path.isdir(os.path.join(root, n))
    )


# ─── orchestrator.json bundle ─────────────────────────────────────────────────

def save_orchestrator_bundle(slug: str, data: Dict[str, Any]) -> bool:
    """Write the orchestrator.json bundle for an orchestrator.

    The bundle is a complete export dict (same schema as export_orchestrator):
    slug, name, description, prompt_text, config, tools, max_steps, auto_apply,
    instructions, functions, exported_at. The prompt_text is also written to
    system_prompt.md for easy direct editing.
    """
    if not isinstance(data, dict):
        return False
    try:
        d = ensure_orchestrator_dir(slug)
        bundle = dict(data)
        bundle.setdefault("slug", slug)
        bundle.setdefault("exported_at", datetime.now().isoformat())
        path = os.path.join(d, "orchestrator.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False, indent=2)
        prompt = data.get("prompt_text")
        if isinstance(prompt, str):
            with open(os.path.join(d, "system_prompt.md"), "w", encoding="utf-8") as f:
                f.write(prompt)
        return True
    except Exception:
        return False


def load_orchestrator_bundle(slug: str) -> Optional[Dict[str, Any]]:
    """Read the orchestrator bundle, or None if neither file is present/valid.

    Prefers the runtime canonical orchestrator.json (export format). When it
    is missing, falls back to the defaults-style settings.json layout
    (metadata + explicit or flat ``config``, prompt read from
    system_prompt.md) so a folder copied from defaults/orchestrators/
    loads directly. Saving always writes orchestrator.json.
    """
    path = os.path.join(get_orchestrator_dir(slug), "orchestrator.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    settings_path = os.path.join(get_orchestrator_dir(slug), "settings.json")
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except Exception:
        return None
    if not isinstance(settings, dict):
        return None
    bundle = dict(settings)
    bundle.setdefault("format", "sagaai_orchestrator/v1")
    bundle.setdefault("slug", slug)
    bundle.setdefault("name", slug)
    bundle.setdefault("description", "")
    bundle.setdefault("tools", [])
    bundle.setdefault("max_steps", 100)
    bundle.setdefault("auto_apply", True)
    bundle.setdefault("is_builtin", False)
    bundle.setdefault("sort_order", 0)
    if not isinstance(bundle.get("tools"), list):
        bundle["tools"] = []
    if not isinstance(bundle.get("config"), dict):
        _meta = {"name", "description", "slug", "format", "prompt_text",
                 "prompt_file", "tools", "max_steps", "auto_apply",
                 "is_builtin", "sort_order", "config", "exported_at"}
        bundle["config"] = {k: v for k, v in settings.items() if k not in _meta}
    if not isinstance(bundle.get("prompt_text"), str) or not bundle.get("prompt_text").strip():
        prompt = load_orchestrator_prompt_file(slug)
        if prompt.strip():
            bundle["prompt_text"] = prompt
    return bundle


def load_orchestrator_prompt_file(slug: str) -> str:
    """Read system_prompt.md next to the bundle ('' when missing)."""
    return read_text_file(os.path.join(get_orchestrator_dir(slug), "system_prompt.md"), "")


# ─── Custom functions ─────────────────────────────────────────────────────────

def list_orchestrator_functions(slug: str) -> List[Dict[str, Any]]:
    """Return metadata for all custom Python functions of an orchestrator.

    Each entry: {"name": str, "path": str, "size_bytes": int, "updated_at": str}
    """
    func_dir = os.path.join(get_orchestrator_dir(slug), "functions")
    if not os.path.isdir(func_dir):
        return []
    result = []
    for fname in sorted(os.listdir(func_dir)):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(func_dir, fname)
        try:
            stat = os.stat(fpath)
            updated = datetime.fromtimestamp(stat.st_mtime).isoformat()
        except Exception:
            stat = None
            updated = ""
        result.append({
            "name": fname[:-3],
            "path": os.path.join("functions", fname),
            "size_bytes": stat.st_size if stat else 0,
            "updated_at": updated,
        })
    return result


def get_orchestrator_function(slug: str, name: str) -> Optional[Dict[str, Any]]:
    """Return a custom function dict: {name, path, code, size_bytes, updated_at}."""
    safe_name = (name or "").strip()
    if not safe_name or not safe_name.isidentifier():
        return None
    fpath = os.path.join(get_orchestrator_dir(slug), "functions", f"{safe_name}.py")
    if not os.path.isfile(fpath):
        return None
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            code = f.read()
        stat = os.stat(fpath)
        return {
            "name": safe_name,
            "path": os.path.join("functions", f"{safe_name}.py"),
            "code": code,
            "size_bytes": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }
    except Exception:
        return None


def save_orchestrator_function(slug: str, name: str, code: str) -> bool:
    """Create or overwrite a custom Python function file.

    The name must be a valid Python identifier. The code must define a top-level
    callable named ``invoke(**kwargs) -> dict`` so the dispatcher can load it
    as a tool.
    """
    safe_name = (name or "").strip()
    if not safe_name or not safe_name.isidentifier():
        return False
    if not code or not code.strip():
        return False
    d = ensure_orchestrator_dir(slug)
    fpath = os.path.join(d, "functions", f"{safe_name}.py")
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(code)
        return True
    except Exception:
        return False


def delete_orchestrator_function(slug: str, name: str) -> bool:
    """Delete a custom function file by name. Returns True on success."""
    safe_name = (name or "").strip()
    if not safe_name or not safe_name.isidentifier():
        return False
    fpath = os.path.join(get_orchestrator_dir(slug), "functions", f"{safe_name}.py")
    try:
        if os.path.isfile(fpath):
            os.remove(fpath)
        return True
    except Exception:
        return False


def load_orchestrator_function_module(slug: str, name: str) -> Optional[Callable[..., Dict[str, Any]]]:
    """Import a custom function file and return its ``invoke`` callable.

    Uses importlib to load the module from the orchestrator folder by file path.
    Returns None if the file is missing or does not define a callable ``invoke``.
    """
    import importlib.util
    safe_name = (name or "").strip()
    if not safe_name or not safe_name.isidentifier():
        return None
    fpath = os.path.join(get_orchestrator_dir(slug), "functions", f"{safe_name}.py")
    if not os.path.isfile(fpath):
        return None
    try:
        module_name = f"_orch_{slug}_{safe_name}"
        spec = importlib.util.spec_from_file_location(module_name, fpath)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = getattr(module, "invoke", None)
        if fn is None or not callable(fn):
            return None
        return fn
    except Exception:
        return None


def load_all_orchestrator_functions(slug: str) -> Dict[str, Callable[..., Dict[str, Any]]]:
    """Load every custom function of an orchestrator as {name: callable}."""
    result: Dict[str, Callable[..., Dict[str, Any]]] = {}
    for meta in list_orchestrator_functions(slug):
        fn = load_orchestrator_function_module(slug, meta["name"])
        if fn is not None:
            result[meta["name"]] = fn
    return result


# ─── Orchestrator-specific instructions (md files + DB cache) ─────────────────

def _instructions_dir(slug: str) -> str:
    return os.path.join(get_orchestrator_dir(slug), "instructions")


def _safe_filename(instruction_id: str) -> str:
    """Sanitize an instruction id so it is safe as a file name."""
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", (instruction_id or "").strip())
    return safe or "instruction"


def _md_path(slug: str, instruction_id: str) -> str:
    return os.path.join(_instructions_dir(slug), f"{_safe_filename(instruction_id)}.md")


def _legacy_instructions_json_path(slug: str) -> str:
    return os.path.join(get_orchestrator_dir(slug), "instructions.json")


def _migrate_instructions_json_to_md(slug: str) -> int:
    """One-time migration of legacy instructions.json into instructions/*.md.

    Runs only when no md files exist yet and the JSON file is present. The
    JSON file is kept for backward compatibility but is no longer read once
    md files exist. Returns the number of migrated instructions.
    """
    md_dir = _instructions_dir(slug)
    if os.path.isdir(md_dir) and any(
        fname.endswith(".md") for fname in os.listdir(md_dir)
    ):
        return 0  # md storage already in use
    json_path = _legacy_instructions_json_path(slug)
    data = read_json_file(json_path, None)
    if not isinstance(data, dict):
        return 0
    ensure_dir(md_dir)
    migrated = 0
    for iid, inst in data.items():
        if not isinstance(inst, dict):
            continue
        ok = _write_instruction_md(
            slug, str(iid),
            name=str(inst.get("name") or iid),
            description=str(inst.get("description") or ""),
            prompt_text=str(inst.get("prompt_text") or ""),
        )
        if ok:
            migrated += 1
    return migrated


def _write_instruction_md(slug: str, instruction_id: str, name: str,
                          description: str, prompt_text: str) -> bool:
    """Write one instruction as a markdown file with front-matter."""
    ensure_dir(_instructions_dir(slug))
    header = [
        "---",
        f"id: {instruction_id}",
        f"name: {name}",
        f"description: {description}",
        "---",
        "",
    ]
    content = "\n".join(header) + (prompt_text or "")
    try:
        with open(_md_path(slug, instruction_id), "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception:
        return False


def _read_instructions_from_folder(slug: str) -> Dict[str, Dict[str, Any]]:
    """Read all instruction md files from disk as {id: {name, description, prompt_text}}.

    This is a filesystem read used only for cache rebuilds and exports, never
    in the runtime hot path.
    """
    _migrate_instructions_json_to_md(slug)
    from core.defaults import parse_front_matter
    result: Dict[str, Dict[str, Any]] = {}
    md_dir = _instructions_dir(slug)
    if not os.path.isdir(md_dir):
        return result
    for fname in sorted(os.listdir(md_dir)):
        if not fname.endswith(".md"):
            continue
        raw = read_text_file(os.path.join(md_dir, fname), "")
        if not raw.strip():
            continue
        default_id = fname[:-3]
        meta, body = parse_front_matter(raw, default_id=default_id)
        iid = meta.get("id") or default_id
        result[iid] = {
            "name": meta.get("name") or iid,
            "description": meta.get("description", ""),
            "prompt_text": body,
        }
    return result


def sync_orchestrator_instructions(slug: str) -> int:
    """Rebuild the DB cache for an orchestrator's instructions from its folder.

    Called at startup, after folder imports, and by the settings "Sync" button.
    Returns the number of cached instructions.
    """
    repo_delete_all_orchestrator_instructions(slug)
    data = _read_instructions_from_folder(slug)
    count = 0
    for iid, inst in data.items():
        if repo_save_orchestrator_instruction(
            slug, iid,
            name=inst.get("name", iid),
            description=inst.get("description", ""),
            prompt_text=inst.get("prompt_text", ""),
        ):
            count += 1
    return count


def list_orchestrator_instructions(slug: str) -> List[Dict[str, Any]]:
    """Return metadata for all orchestrator-specific instructions (from DB cache).

    On first use (empty cache) the cache is rebuilt from the folder once.
    The hot path afterwards reads only from the database.
    """
    cached = repo_list_orchestrator_instructions(slug)
    if cached:
        return cached
    sync_orchestrator_instructions(slug)
    return repo_list_orchestrator_instructions(slug)


def get_orchestrator_instruction(slug: str, instruction_id: str) -> Optional[Dict[str, Any]]:
    """Return a full orchestrator instruction including prompt_text (from DB cache), or None."""
    row = repo_get_orchestrator_instruction(slug, instruction_id)
    if row is None:
        return None
    return {
        "id": instruction_id,
        "name": row.get("name", instruction_id),
        "description": row.get("description", ""),
        "text": row.get("prompt_text", ""),
    }


def save_orchestrator_instruction(
    slug: str,
    instruction_id: str,
    name: str,
    description: str = "",
    prompt_text: str = "",
) -> str:
    """Create or update an orchestrator-specific instruction.

    Writes the md file and refreshes the DB cache entry.
    instruction_id may be empty/None - a random 8-char hex id is generated.

    Returns the EFFECTIVE instruction id (the passed id, or the generated
    one) on success, so callers know how to address the instruction later.
    Returns '' on failure. The return value is falsey exactly when the save
    failed, so boolean checks (``if save_orchestrator_instruction(...):``)
    keep working.
    """
    iid = (instruction_id or "").strip()
    if not iid:
        iid = uuid.uuid4().hex[:8]
    effective_name = (name or iid).strip()
    effective_desc = description or ""
    effective_text = prompt_text or ""
    ok_file = _write_instruction_md(
        slug, iid,
        name=effective_name,
        description=effective_desc,
        prompt_text=effective_text,
    )
    ok_cache = repo_save_orchestrator_instruction(
        slug, iid,
        name=effective_name,
        description=effective_desc,
        prompt_text=effective_text,
    )
    if ok_file and ok_cache:
        return iid
    return ""


def delete_orchestrator_instruction(slug: str, instruction_id: str) -> bool:
    """Delete an orchestrator-specific instruction.

    Removes the md file and the DB cache entry. Returns True when the
    instruction no longer exists after the call.
    """
    iid = (instruction_id or "").strip()
    if not iid:
        return False
    fpath = _md_path(slug, iid)
    try:
        if os.path.isfile(fpath):
            os.remove(fpath)
    except Exception:
        pass
    repo_delete_orchestrator_instruction(slug, iid)
    return not os.path.isfile(fpath)


# ─── Export / import of the whole folder ─────────────────────────────────────

def export_orchestrator_folder(slug: str) -> Optional[Dict[str, Any]]:
    """Return a complete, JSON-serializable dict of an orchestrator's folder.

    Includes orchestrator.json contents plus the raw code of every custom
    function and all instructions (read from the md files). Returns None if
    the folder does not exist.
    """
    if not orchestrator_folder_exists(slug):
        return None
    bundle = load_orchestrator_bundle(slug) or {}
    functions = {}
    for meta in list_orchestrator_functions(slug):
        fn = get_orchestrator_function(slug, meta["name"])
        if fn:
            functions[meta["name"]] = fn["code"]
    instructions = _read_instructions_from_folder(slug)
    return {
        "bundle": bundle,
        "functions": functions,
        "instructions": instructions,
    }


def import_orchestrator_folder(slug: str, data: Dict[str, Any]) -> bool:
    """Recreate an orchestrator folder from an export dict.

    Overwrites existing files. Expects the dict shape produced by
    export_orchestrator_folder(). The instructions cache is rebuilt after
    the import. Returns True on success.
    """
    try:
        d = ensure_orchestrator_dir(slug)
        # Write bundle (if present).
        bundle = data.get("bundle")
        if isinstance(bundle, dict):
            with open(os.path.join(d, "orchestrator.json"), "w", encoding="utf-8") as f:
                json.dump(bundle, f, ensure_ascii=False, indent=2)
            prompt = bundle.get("prompt_text")
            if isinstance(prompt, str):
                with open(os.path.join(d, "system_prompt.md"), "w", encoding="utf-8") as f:
                    f.write(prompt)
        # Write functions.
        func_dir = os.path.join(d, "functions")
        ensure_dir(func_dir)
        functions = data.get("functions", {})
        if isinstance(functions, dict):
            for fname, code in functions.items():
                safe = (fname or "").strip()
                if safe and safe.isidentifier() and isinstance(code, str):
                    with open(os.path.join(func_dir, f"{safe}.py"), "w", encoding="utf-8") as f:
                        f.write(code)
        # Write instructions as md files.
        instructions = data.get("instructions", {})
        if isinstance(instructions, dict):
            for iid, inst in instructions.items():
                if isinstance(iid, str) and isinstance(inst, dict):
                    _write_instruction_md(
                        slug, iid,
                        name=str(inst.get("name") or iid),
                        description=str(inst.get("description") or ""),
                        prompt_text=str(inst.get("prompt_text") or ""),
                    )
        sync_orchestrator_instructions(slug)
        return True
    except Exception:
        return False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
