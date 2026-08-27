# -*- coding: utf-8 -*-
"""
core.assistant_folders - file-system storage for assistant profiles.

Each assistant has its own folder under DATA_DIR/assistants/<slug>/:

    manifest.json           - name, slug, service, model, temperature, tools,
                              max_tool_calls, max_tokens, description
    prompt.md               - the assistant's system prompt text
    files/                  - optional attachment files (stored as .txt)

Keeping everything in a folder makes export/import trivial: the folder can be
zipped or copied. DevAgent can read/edit these files directly, which allows
iteration on assistant behaviour through the normal DevAgent workflow.

The folder is the *source of truth* for an assistant's content. The DB record
(system_prompts / assistants table) is treated as a runtime cache: it is
rebuilt from the folder during startup sync and refreshed whenever content
changes through the UI or through DevAgent.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import core.paths
from core.fs import ensure_dir, read_json_file, read_text_file, write_json_file, write_text_file


# ─── Slug helpers ─────────────────────────────────────────────────────────────

_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
    "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}


def normalize_slug(name: str, fallback: str = "assistant") -> str:
    """Convert a display name into a filesystem-safe slug.

    Lowercases, transliterates Cyrillic letters to Latin (so Russian names
    keep readable slugs like «Редактор текста» -> redaktor_teksta), replaces
    remaining non-ASCII characters and spaces/dashes with underscores. Falls
    back to *fallback* when empty.
    """
    s = (name or "").strip().lower()
    s = "".join(_CYR_TO_LAT.get(ch, ch) for ch in s)
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = s.strip("_")
    return s or fallback


# ─── Path helpers ─────────────────────────────────────────────────────────────

def get_assistants_root() -> str:
    """Return the root directory where all assistant folders live."""
    return os.path.join(core.paths.DATA_DIR, "assistants")


def get_assistant_dir(slug: str) -> str:
    """Return the folder path for a single assistant (not necessarily existing)."""
    safe = normalize_slug(slug)
    return os.path.join(get_assistants_root(), safe)


def ensure_assistant_dir(slug: str) -> str:
    """Create and return the assistant folder path."""
    d = get_assistant_dir(slug)
    ensure_dir(d)
    ensure_dir(os.path.join(d, "files"))
    return d


def remove_assistant_dir(slug: str) -> bool:
    """Recursively delete the assistant folder. Returns True on success."""
    d = get_assistant_dir(slug)
    try:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        return True
    except Exception:
        return False


def assistant_folder_exists(slug: str) -> bool:
    """True if the assistant already has a personal folder on disk."""
    return os.path.isdir(get_assistant_dir(slug))


def list_assistant_folder_names() -> List[str]:
    """Return sorted folder names of all assistant folders on disk."""
    root = get_assistants_root()
    if not os.path.isdir(root):
        return []
    return sorted(
        n for n in os.listdir(root)
        if not n.startswith(".") and os.path.isdir(os.path.join(root, n))
    )


# ─── manifest.json bundle ─────────────────────────────────────────────────────

def save_assistant_bundle(slug: str, data: Dict[str, Any]) -> bool:
    """Write the manifest.json bundle for an assistant.

    The manifest is a flat dict with the assistant's metadata (name, slug,
    service, model, temperature, description, tools, max_tool_calls,
    max_tokens). It does not include the prompt text (kept in prompt.md).
    """
    if not isinstance(data, dict):
        return False
    try:
        d = ensure_assistant_dir(slug)
        bundle = dict(data)
        bundle.setdefault("slug", slug)
        bundle.setdefault("updated_at", datetime.now().isoformat())
        path = os.path.join(d, "manifest.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def load_assistant_bundle(slug: str) -> Optional[Dict[str, Any]]:
    """Read the assistant bundle, or None if it does not exist/invalid.

    Prefers the runtime canonical manifest.json. Falls back to the
    defaults-style settings.json so a folder copied from defaults/assistants/
    loads directly. Saving always writes manifest.json.
    """
    path = os.path.join(get_assistant_dir(slug), "manifest.json")
    data = read_json_file(path, None)
    if isinstance(data, dict):
        return data
    alt = os.path.join(get_assistant_dir(slug), "settings.json")
    alt_data = read_json_file(alt, None)
    return alt_data if isinstance(alt_data, dict) else None


# ─── prompt.md ────────────────────────────────────────────────────────────────

def get_assistant_prompt_path(slug: str) -> str:
    """Return the prompt.md path for an assistant."""
    return os.path.join(get_assistant_dir(slug), "prompt.md")


def save_assistant_prompt(slug: str, text: str) -> bool:
    """Write the assistant's prompt.md file. Returns True on success."""
    d = ensure_assistant_dir(slug)
    return write_text_file(os.path.join(d, "prompt.md"), text or "")


def load_assistant_prompt(slug: str) -> str:
    """Read the assistant's prompt.md file ('' if missing/unreadable)."""
    return read_text_file(get_assistant_prompt_path(slug), "")


# ─── Attachment files ─────────────────────────────────────────────────────────

def list_assistant_files(slug: str) -> List[str]:
    """Return sorted list of filenames in the assistant's files/ folder."""
    d = os.path.join(get_assistant_dir(slug), "files")
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n)))


def save_assistant_file(slug: str, filename: str, content: str) -> bool:
    """Save a text file to the assistant's files/ folder. Returns True on success."""
    safe_name = (filename or "").strip()
    if not safe_name:
        return False
    if not safe_name.endswith(".txt"):
        safe_name += ".txt"
    d = ensure_dir(os.path.join(ensure_assistant_dir(slug), "files"))
    return write_text_file(os.path.join(d, safe_name), content or "")


def delete_assistant_file(slug: str, stored_name: str) -> bool:
    """Delete a named file from the assistant's files/ folder."""
    fpath = os.path.join(get_assistant_dir(slug), "files", (stored_name or "").strip())
    try:
        if os.path.isfile(fpath):
            os.remove(fpath)
        return True
    except Exception:
        return False


def load_assistant_file_content(slug: str, stored_name: str) -> str:
    """Read one attachment file's text ('' when missing)."""
    fpath = os.path.join(get_assistant_dir(slug), "files", (stored_name or "").strip())
    return read_text_file(fpath, "")


def load_all_assistant_files(slug: str) -> Dict[str, str]:
    """Return all attachment files of an assistant as {filename: content}."""
    result: Dict[str, str] = {}
    for fname in list_assistant_files(slug):
        content = load_assistant_file_content(slug, fname)
        if content:
            result[fname] = content
    return result


# ─── Export / import of the whole folder ─────────────────────────────────────

def export_assistant_folder(slug: str) -> Optional[Dict[str, Any]]:
    """Return a complete, JSON-serializable dict of an assistant's folder.

    Includes manifest.json contents, the prompt text and all attachment
    files. Returns None if the folder does not exist.
    """
    if not assistant_folder_exists(slug):
        return None
    bundle = load_assistant_bundle(slug) or {}
    return {
        "bundle": bundle,
        "prompt_text": load_assistant_prompt(slug),
        "files": load_all_assistant_files(slug),
    }


def import_assistant_folder(slug: str, data: Dict[str, Any]) -> bool:
    """Recreate an assistant folder from an export dict.

    Overwrites existing files. Expects the dict shape produced by
    export_assistant_folder(). Returns True on success.
    """
    try:
        d = ensure_assistant_dir(slug)
        bundle = data.get("bundle")
        if isinstance(bundle, dict):
            save_assistant_bundle(slug, bundle)
        prompt = data.get("prompt_text")
        if isinstance(prompt, str):
            save_assistant_prompt(slug, prompt)
        files = data.get("files")
        if isinstance(files, dict):
            # Clear stale files first so removed attachments do not linger.
            files_dir = os.path.join(d, "files")
            if os.path.isdir(files_dir):
                shutil.rmtree(files_dir, ignore_errors=True)
            ensure_dir(files_dir)
            for fname, content in files.items():
                if isinstance(fname, str) and isinstance(content, str):
                    save_assistant_file(slug, fname, content)
        return True
    except Exception:
        return False


# ─── Sync to/from the DB ──────────────────────────────────────────────────────

def build_manifest_from_assistant(assistant: Dict[str, Any]) -> Dict[str, Any]:
    """Build a manifest dict from a DB assistant dict (without prompt text)."""
    return {
        "id": assistant.get("id", ""),
        "slug": assistant.get("slug") or normalize_slug(assistant.get("name", "")),
        "name": assistant.get("name", ""),
        "service": assistant.get("service", ""),
        "model": assistant.get("model", ""),
        "temperature": assistant.get("temperature", 0.3),
        "description": assistant.get("description", ""),
        "tools": assistant.get("tools") or [],
        "max_tool_calls": assistant.get("max_tool_calls"),
        "max_tokens": assistant.get("max_tokens"),
        "reasoning_effort": assistant.get("reasoning_effort"),
    }


def sync_assistant_to_folder(assistant: Dict[str, Any]) -> bool:
    """Write the current DB state of an assistant into its folder.

    The assistant dict must include: id, slug, name, service, model,
    temperature, description, tools, max_tool_calls, max_tokens, text
    (prompt text). Returns True on success.

    The manifest preserves local-only metadata (``rag_bases`` binding) that
    is kept in the folder and not stored in the DB. When a previous manifest
    exists, its ``rag_bases`` list is carried over so syncing does not drop
    assistant/RAG bindings.
    """
    slug = assistant.get("slug") or normalize_slug(assistant.get("name", ""))
    if not slug:
        return False
    d = ensure_assistant_dir(slug)
    bundle = build_manifest_from_assistant(assistant)
    prev = load_assistant_bundle(slug)
    if isinstance(prev, dict):
        if prev.get("rag_bases"):
            bundle["rag_bases"] = list(prev["rag_bases"])
        # Preserve per-assistant web-search overrides across DB re-syncs.
        if prev.get("web_search_context_size") in ("low", "medium", "high"):
            bundle["web_search_context_size"] = prev["web_search_context_size"]
        prev_domains = _normalize_domain_list(prev.get("web_search_allowed_domains") or [])
        if prev_domains:
            bundle["web_search_allowed_domains"] = prev_domains
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    with open(os.path.join(d, "prompt.md"), "w", encoding="utf-8") as f:
        f.write(assistant.get("text", "") or "")
    # Copy attachment files from the legacy system_prompts/<id>/files layout
    # to the new folder when they exist there and the folder has none yet.
    _copy_legacy_files_if_needed(assistant.get("id", ""), slug, d)
    return True


def set_assistant_rag_bases(slug: str, bases: list) -> bool:
    """Persist the assistant's RAG-base bindings into its folder manifest.

    *bases* is a list of base slugs; duplicates are dropped and each entry is
    normalized to lowercase. An empty list removes the binding entirely
    (the key is stored as an empty list so the semantics stay explicit).
    Returns True on success, False when the assistant folder is missing.
    """
    bundle = load_assistant_bundle(slug)
    if bundle is None:
        return False
    clean: list = []
    for b in (bases or []):
        val = str(b or "").strip().lower()
        if val and val not in clean:
            clean.append(val)
    bundle["rag_bases"] = clean
    return save_assistant_bundle(slug, bundle)


def _normalize_domain_list(value) -> list:
    """Normalize a user-supplied domain list (string or list) to clean strings.

    Accepts a comma/space/semicolon-separated string or an iterable. Entries
    are lowercased, stripped and de-duplicated. Invalid entries are dropped.
    """
    if isinstance(value, str):
        parts = re.split(r"[,\s;]+", value)
    else:
        parts = value or []
    result: list = []
    for p in parts:
        val = str(p or "").strip().lower().strip(".")
        if val and val not in result:
            result.append(val)
    return result


def set_assistant_web_search_settings(slug: str, context_size=None, allowed_domains=None) -> bool:
    """Persist per-assistant web-search overrides into the folder manifest.

    *context_size* must be one of ``low``/``medium``/``high`` (empty/None
    removes the override). *allowed_domains* accepts a comma/space separated
    string or a list; empty/None removes the override. Returns True on
    success, False when the assistant folder is missing.
    """
    bundle = load_assistant_bundle(slug)
    if bundle is None:
        return False
    if context_size is not None:
        val = str(context_size or "").strip().lower()
        if val in ("low", "medium", "high"):
            bundle["web_search_context_size"] = val
        else:
            bundle.pop("web_search_context_size", None)
    if allowed_domains is not None:
        domains = _normalize_domain_list(allowed_domains)
        if domains:
            bundle["web_search_allowed_domains"] = domains
        else:
            bundle.pop("web_search_allowed_domains", None)
    return save_assistant_bundle(slug, bundle)


def get_assistant_web_search_settings(slug: str) -> Dict[str, Any]:
    """Return per-assistant web-search overrides as a dict.

    Returns the override keys ``context_size`` (one of low/medium/high) and
    ``allowed_domains`` (list) only when they are set in the manifest; an
    empty dict means the assistant inherits provider-level defaults.
    """
    bundle = load_assistant_bundle(slug)
    if not isinstance(bundle, dict):
        return {}
    result: Dict[str, Any] = {}
    if bundle.get("web_search_context_size") in ("low", "medium", "high"):
        result["context_size"] = bundle["web_search_context_size"]
    domains = _normalize_domain_list(bundle.get("web_search_allowed_domains") or [])
    if domains:
        result["allowed_domains"] = domains
    return result


def _copy_legacy_files_if_needed(assistant_id: str, slug: str, folder: str) -> None:
    """One-time migration of attachment files from the legacy layout.

    The old layout stored files under <user_data>/system_prompts/<id>/files/.
    If the new assistant folder has no files yet, any files found in the
    legacy location are copied over.
    """
    try:
        from core.assistants import get_assistant_files_dir as legacy_dir
    except Exception:
        return
    try:
        src = legacy_dir(assistant_id)
    except Exception:
        return
    files_dir = os.path.join(folder, "files")
    if not os.path.isdir(src):
        return
    if list_assistant_files(slug):
        return  # already migrated
    ensure_dir(files_dir)
    for fname in sorted(os.listdir(src)):
        fpath = os.path.join(src, fname)
        if os.path.isfile(fpath):
            try:
                shutil.copy2(fpath, os.path.join(files_dir, fname))
            except Exception:
                pass
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
