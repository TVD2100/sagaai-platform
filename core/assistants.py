"""
core.assistants - CRUD and management of AI assistant profiles.
Each assistant defines a system prompt, model, tools, and related metadata.

Since the folder-based storage refactoring, every assistant also has a
personal folder under DATA_DIR/assistants/<slug>/:

    manifest.json    - metadata (name, slug, service, model, temperature,
                       tools, max_tool_calls, max_tokens, description)
    prompt.md        - the system prompt text
    files/           - optional attachment files

The folder is the *source of truth* for an assistant's content. The DB row
is a runtime cache that is synced from the folder on startup and refreshed
whenever content changes through the UI or through DevAgent.
"""
import json
import os
import re
import shutil
import uuid
from datetime import datetime

from storage.repository import (
    repo_create_assistant,
    repo_update_assistant,
    repo_delete_assistant,
    repo_load_assistants,
    repo_get_assistant_with_text,
    repo_get_assistant_by_slug,
    repo_save_assistant_prompt_text,
)
from core.assistant_folders import (
    normalize_slug,
    get_assistant_dir,
    ensure_assistant_dir,
    remove_assistant_dir,
    assistant_folder_exists,
    sync_assistant_to_folder,
    export_assistant_folder,
    import_assistant_folder,
    load_assistant_bundle,
    load_assistant_prompt,
    list_assistant_files as folder_list_files,
    save_assistant_file as folder_save_file,
    delete_assistant_file as folder_delete_file,
    load_assistant_file_content as folder_load_file,
    load_all_assistant_files as folder_load_all_files,
)
from core.config import load_config

# ─── Paths ────────────────────────────────────────────────────────────────

def _get_user_data_dir() -> str:
    """Return the user-data directory from config, or a safe default."""
    cfg = load_config()
    return cfg.get("user_data_dir") or os.path.join(os.path.expanduser("~"), ".sagaai")

def ensure_dir(path: str) -> str:
    """Create a directory tree if absent; return the path.

    Kept for backward compatibility with core.skills (legacy re-export).
    """
    os.makedirs(path, exist_ok=True)
    return path

LEGACY_SYSTEM_PROMPTS_DIR = os.path.join(_get_user_data_dir(), "system_prompts")
os.makedirs(LEGACY_SYSTEM_PROMPTS_DIR, exist_ok=True)

# Backward-compatible alias (legacy name; new code should use
# core.assistant_folders.get_assistants_root()).
SYSTEM_PROMPTS_DIR = LEGACY_SYSTEM_PROMPTS_DIR


# ─── Slug helpers ─────────────────────────────────────────────────────────

def _unique_slug(name: str) -> str:
    """Return a unique slug derived from *name*.

    Uses normalize_slug() and appends a numeric suffix (_2, _3, ...) when a
    slug is already taken by another assistant.
    """
    base = normalize_slug(name)
    if not base:
        base = "assistant"
    candidate = base
    suffix = 2
    while repo_get_assistant_by_slug(candidate):
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


# ─── CRUD helpers ─────────────────────────────────────────────────────────

def load_assistants_index() -> list:
    """Return all assistants (without prompt_text) from the DB."""
    return repo_load_assistants()


def load_assistant_prompt_text(assistant_id: str) -> str:
    """Return the full prompt text for a given assistant id."""
    from storage.repository import repo_load_assistant_prompt_text
    return repo_load_assistant_prompt_text(assistant_id)


def save_assistant_prompt_text(assistant_id: str, text: str) -> bool:
    """Overwrite the prompt text for an existing assistant.

    Updates both the DB row and the assistant's prompt.md file.
    Returns True on success.
    """
    ok = repo_save_assistant_prompt_text(assistant_id, text)
    if ok:
        full = repo_get_assistant_with_text(assistant_id)
        if full:
            sync_assistant_to_folder(full)
    return ok


# ─── Full assistant retrieval ─────────────────────────────────────────────

def get_assistant_by_id(assistant_id: str) -> dict | None:
    """
    Return an assistant dict with an extra 'text' key containing the prompt text.
    Returns None if not found.
    """
    return repo_get_assistant_with_text(assistant_id)


def get_assistant_by_slug(slug: str) -> dict | None:
    """Return an assistant dict (without prompt_text) by slug, or None."""
    return repo_get_assistant_by_slug(slug)


# ─── CRUD ─────────────────────────────────────────────────────────────────

def create_assistant(name: str, service: str, model: str,
                     temperature, text: str, description: str = "",
                     tools: list = None, max_tool_calls: int = None,
                     max_tokens: int = None,
                     reasoning_effort: str = None) -> str | None:
    """
    Create a new assistant. Returns the 8-char assistant ID on success, None on failure.

    The assistant gets a unique slug and its own folder
    (DATA_DIR/assistants/<slug>/) with manifest.json + prompt.md. Attachment
    files from the legacy layout are migrated into the folder.
    """
    if tools is None:
        tools = []
    slug = _unique_slug(name)
    pid = str(uuid.uuid4())[:8]
    ok = repo_create_assistant(
        assistant_id=pid, slug=slug, name=name, service=service, model=model,
        temperature=float(temperature), prompt_text=text, description=description,
        tools=tools, max_tool_calls=max_tool_calls, max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
    )
    if ok:
        full = repo_get_assistant_with_text(pid)
        if full:
            sync_assistant_to_folder(full)
    return pid if ok else None


def update_assistant(pid: str, name: str, service: str, model: str,
                     temperature, text: str, description: str = "",
                     tools: list = None, max_tool_calls: int = None,
                     max_tokens: int = None,
                     reasoning_effort: str = None) -> bool:
    """Update an existing assistant. Returns True on success.

    The assistant's folder is resynced from the new DB state.
    """
    if tools is None:
        tools = []
    ok = repo_update_assistant(
        assistant_id=pid, name=name, service=service, model=model,
        temperature=float(temperature), prompt_text=text, description=description,
        tools=tools, max_tool_calls=max_tool_calls, max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
    )
    if ok:
        full = repo_get_assistant_with_text(pid)
        if full:
            sync_assistant_to_folder(full)
    return ok


def delete_assistant(pid: str) -> bool:
    """
    Delete an assistant from the DB and remove its folder from disk.
    Returns True on success.
    """
    full = repo_get_assistant_with_text(pid)
    if full and full.get("slug"):
        remove_assistant_dir(full["slug"])
    legacy_dir = get_legacy_assistant_files_dir(pid)
    if os.path.isdir(legacy_dir):
        shutil.rmtree(legacy_dir, ignore_errors=True)
    return repo_delete_assistant(pid)


# ─── Assistant file helpers ───────────────────────────────────────────────

def get_legacy_assistant_files_dir(pid: str) -> str:
    """Return the legacy attachment directory: system_prompts/{pid}/files/"""
    return os.path.join(LEGACY_SYSTEM_PROMPTS_DIR, pid, "files")


def get_assistant_files_dir(pid: str) -> str:
    """Return the assistant's attachment directory.

    New assistants use DATA_DIR/assistants/<slug>/files. For assistants that
    have not been migrated yet (no slug in the DB), falls back to the legacy
    system_prompts/<id>/files layout.
    """
    full = repo_get_assistant_with_text(pid)
    if full and full.get("slug"):
        return os.path.join(get_assistant_dir(full["slug"]), "files")
    return get_legacy_assistant_files_dir(pid)


def list_assistant_files(pid: str) -> list:
    """Return sorted list of filenames in the assistant's files directory."""
    full = repo_get_assistant_with_text(pid)
    if full and full.get("slug"):
        return folder_list_files(full["slug"])
    d = get_legacy_assistant_files_dir(pid)
    if not os.path.isdir(d):
        return []
    return sorted(os.listdir(d))


def save_assistant_file(pid: str, filename: str, content: str) -> bool:
    """Save a text file to the assistant's files directory. Returns True on success."""
    full = repo_get_assistant_with_text(pid)
    if full and full.get("slug"):
        return folder_save_file(full["slug"], filename, content)
    d = os.path.join(get_legacy_assistant_files_dir(pid))
    os.makedirs(d, exist_ok=True)
    fname = filename
    if not fname.endswith(".txt"):
        fname += ".txt"
    try:
        with open(os.path.join(d, fname), "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception:
        return False


def delete_assistant_file(pid: str, stored_name: str) -> bool:
    """Delete a named file from the assistant's files directory. Returns True on success."""
    full = repo_get_assistant_with_text(pid)
    if full and full.get("slug"):
        return folder_delete_file(full["slug"], stored_name)
    fpath = os.path.join(get_legacy_assistant_files_dir(pid), stored_name)
    try:
        if os.path.exists(fpath):
            os.remove(fpath)
        return True
    except Exception:
        return False


def load_assistant_files_context(pid: str) -> str:
    """Load all attachment files for an assistant and concatenate them as context text."""
    files = {}
    full = repo_get_assistant_with_text(pid)
    if full and full.get("slug"):
        files = folder_load_all_files(full["slug"])
    else:
        d = get_legacy_assistant_files_dir(pid)
        if os.path.isdir(d):
            for fname in sorted(os.listdir(d)):
                try:
                    with open(os.path.join(d, fname), encoding="utf-8") as f:
                        files[fname] = f.read()
                except Exception:
                    pass
    parts = []
    for fname, content in files.items():
        if not content:
            continue
        display_name = fname[:-4] if fname.endswith(".txt") else fname
        parts.append(f"### File: {display_name}\n{content}")
    return "\n\n".join(parts)


# ─── Folder import / export / reload ──────────────────────────────────────

def export_assistant(assistant_id: str) -> dict | None:
    """Export an assistant as a JSON-serializable dict (folder export)."""
    full = repo_get_assistant_with_text(assistant_id)
    if full is None:
        return None
    slug = full.get("slug") or normalize_slug(full.get("name", ""))
    folder_data = export_assistant_folder(slug) if assistant_folder_exists(slug) else None
    return {
        "format": "sagaai_assistant/v1",
        "slug": slug,
        "name": full.get("name", ""),
        "service": full.get("service", ""),
        "model": full.get("model", ""),
        "temperature": full.get("temperature", 0.3),
        "description": full.get("description", ""),
        "prompt_text": full.get("text", ""),
        "tools": full.get("tools", []),
        "max_tool_calls": full.get("max_tool_calls"),
        "max_tokens": full.get("max_tokens"),
        "reasoning_effort": full.get("reasoning_effort"),
        "files": (folder_data or {}).get("files", {}),
        "exported_at": datetime.now().isoformat(),
    }


def import_assistant(data: dict, overwrite: bool = False) -> dict:
    """Import an assistant from an export dict.

    Returns {"ok": bool, "id": str, "slug": str, "action": "created"|"updated",
    "error": str}.
    """
    if not isinstance(data, dict):
        return {"ok": False, "error": "Invalid export data: not a dict.", "slug": ""}
    if data.get("format") != "sagaai_assistant/v1":
        return {"ok": False, "error": "Unknown format.", "slug": ""}

    name = str(data.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "Missing name.", "slug": ""}
    slug = normalize_slug(data.get("slug") or name)
    existing = repo_get_assistant_by_slug(slug)

    prompt_text = str(data.get("prompt_text") or "")
    if not prompt_text.strip():
        return {"ok": False, "error": "Missing prompt_text.", "slug": slug}

    def _apply(pid: str | None) -> dict:
        if pid:
            ok = repo_update_assistant(
                assistant_id=pid, name=name,
                service=str(data.get("service") or ""),
                model=str(data.get("model") or ""),
                temperature=float(data.get("temperature", 0.3) or 0.3),
                prompt_text=prompt_text,
                description=str(data.get("description") or ""),
                tools=list(data.get("tools") or []),
                max_tool_calls=data.get("max_tool_calls"),
                max_tokens=data.get("max_tokens"),
                reasoning_effort=data.get("reasoning_effort"),
            )
            if not ok:
                return {"ok": False, "error": "DB update failed.", "slug": slug}
            full = repo_get_assistant_with_text(pid)
            if full:
                sync_assistant_to_folder(full)
            return {"ok": True, "id": pid, "slug": slug, "action": "updated"}
        new_pid = str(uuid.uuid4())[:8]
        ok = repo_create_assistant(
            assistant_id=new_pid, slug=slug, name=name,
            service=str(data.get("service") or ""),
            model=str(data.get("model") or ""),
            temperature=float(data.get("temperature", 0.3) or 0.3),
            prompt_text=prompt_text,
            description=str(data.get("description") or ""),
            tools=list(data.get("tools") or []),
            max_tool_calls=data.get("max_tool_calls"),
            max_tokens=data.get("max_tokens"),
            reasoning_effort=data.get("reasoning_effort"),
        )
        if not ok:
            return {"ok": False, "error": "DB create failed.", "slug": slug}
        full = repo_get_assistant_with_text(new_pid)
        if full:
            sync_assistant_to_folder(full)
        return {"ok": True, "id": new_pid, "slug": slug, "action": "created"}

    if existing:
        if not overwrite:
            slug2 = slug
            suffix = 2
            while repo_get_assistant_by_slug(slug2):
                slug2 = f"{slug}_{suffix}"
                suffix += 1
            slug = slug2
            return _apply(None)
        return _apply(existing["id"])
    return _apply(None)


def reload_assistant_from_folder(slug: str) -> dict:
    """Reload an assistant from its folder into the DB.

    Reads manifest.json + prompt.md and creates/updates the DB record.
    Returns {"ok": bool, "id": str, "slug": str, "action": "created"|"updated"|"skipped",
    "error": str}.
    """
    bundle = load_assistant_bundle(slug)
    if bundle is None:
        return {"ok": False, "error": "Missing manifest.json", "slug": slug}
    prompt = load_assistant_prompt(slug)
    if not prompt.strip():
        return {"ok": False, "error": "Empty prompt.md", "slug": slug}

    name = str(bundle.get("name") or "").strip() or slug
    existing = repo_get_assistant_by_slug(slug)

    def _apply(pid: str | None) -> dict:
        if pid:
            ok = repo_update_assistant(
                assistant_id=pid, name=name,
                service=str(bundle.get("service") or ""),
                model=str(bundle.get("model") or ""),
                temperature=float(bundle.get("temperature", 0.3) or 0.3),
                prompt_text=prompt,
                description=str(bundle.get("description") or ""),
                tools=list(bundle.get("tools") or []),
                max_tool_calls=bundle.get("max_tool_calls"),
                max_tokens=bundle.get("max_tokens"),
                reasoning_effort=bundle.get("reasoning_effort"),
            )
            if not ok:
                return {"ok": False, "error": "DB update failed.", "slug": slug}
            return {"ok": True, "id": pid, "slug": slug, "action": "updated"}
        new_pid = str(uuid.uuid4())[:8]
        ok = repo_create_assistant(
            assistant_id=new_pid, slug=slug, name=name,
            service=str(bundle.get("service") or ""),
            model=str(bundle.get("model") or ""),
            temperature=float(bundle.get("temperature", 0.3) or 0.3),
            prompt_text=prompt,
            description=str(bundle.get("description") or ""),
            tools=list(bundle.get("tools") or []),
            max_tool_calls=bundle.get("max_tool_calls"),
            max_tokens=bundle.get("max_tokens"),
            reasoning_effort=bundle.get("reasoning_effort"),
        )
        if not ok:
            return {"ok": False, "error": "DB create failed.", "slug": slug}
        return {"ok": True, "id": new_pid, "slug": slug, "action": "created"}

    return _apply(existing["id"] if existing else None)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
