# -*- coding: utf-8 -*-
"""
core.defaults - central management of bundled default data.

The defaults/ folder at the package root is the single place that defines
which entities are imported on first boot:

    defaults/
    ├── settings/global.json          - global defaults (default UI language,
    │                                   providers preset for the settings page)
    ├── orchestrators/<slug>/         - default orchestrators, each in its own
    │   │                                folder (dev_agent, ya_agent, ...):
    │   ├── orchestrator.json        -   orchestrator settings/config
    │   ├── system_prompt.md          -   system prompt text
    │   ├── instructions/*.md         -   orchestrator-specific instructions
    │   └── functions/*.py            -   custom Python functions
    ├── assistants/<name>/            - default assistants:
    │   ├── manifest.json             -   model/temperature/tools/...
    │   ├── prompt.md                 -   system prompt text
    │   └── files/                    -   optional attachment files
    ├── services/*.json               - LLM provider definitions
    ├── langs/*.json, *_guide.md      - UI languages
    └── skills/                       - default standardized skills

Deleting a file or folder under defaults/ excludes the corresponding entity
from the default import. Adding a new file/folder makes it appear on the next
boot. Legacy locations (services/, langs/) that existed before defaults/
was introduced may still be read as fallbacks where explicitly documented;
the presets/ directory is no longer used at all. The old
`default_devagent_config.json` location is gone - the single source for
DevAgent defaults is now the canonical DevAgent orchestrator bundle.
defaults/ is the single source for bundled entities. (The built-in DevAgent
system prompt lives in dev_agent/system_prompt.md and is managed separately;
its default settings live in orchestrators/dev_agent/orchestrator.json.)

This module contains only path helpers and loaders; it has no side effects.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import core.paths


# ─── Directory helpers ────────────────────────────────────────────────────────

def defaults_root() -> str:
    """Return the absolute path of the defaults/ folder."""
    return core.paths.DEFAULTS_DIR


def settings_dir() -> str:
    """Return defaults/settings (global settings)."""
    return os.path.join(defaults_root(), "settings")


def orchestrators_dir() -> str:
    """Return defaults/orchestrators (default orchestrator folders)."""
    return os.path.join(defaults_root(), "orchestrators")


def assistants_dir() -> str:
    """Return defaults/assistants (default assistant folders)."""
    return os.path.join(defaults_root(), "assistants")


def services_dir() -> str:
    """Return defaults/services (default LLM provider definitions)."""
    return os.path.join(defaults_root(), "services")


def langs_dir() -> str:
    """Return defaults/langs (default UI languages)."""
    return os.path.join(defaults_root(), "langs")


def skills_dir() -> str:
    """Return defaults/skills (default standardized skills)."""
    return os.path.join(defaults_root(), "skills")


def rag_bases_dir() -> str:
    """Return defaults/rag_bases (bundled RAG knowledge bases)."""
    return os.path.join(defaults_root(), "rag_bases")


def list_default_rag_base_slugs() -> List[str]:
    """Return slugs (folder names) of default RAG knowledge bases.

    Only directories with a manifest.json inside defaults/rag_bases/ are
    considered.
    """
    root = rag_bases_dir()
    if not os.path.isdir(root):
        return []
    result = []
    for name in sorted(os.listdir(root)):
        if name.startswith("."):
            continue
        if os.path.isdir(os.path.join(root, name)) and os.path.isfile(
            os.path.join(root, name, "manifest.json")
        ):
            result.append(name)
    return result


def exists() -> bool:
    """True when the defaults/ folder exists on disk."""
    return os.path.isdir(defaults_root())


# ─── JSON helpers ─────────────────────────────────────────────────────────────

def read_json(path: str, default: Any = None) -> Any:
    """Read a JSON file; return *default* on any error (missing/corrupt)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ─── Front-matter parsing for .md defaults ───────────────────────────────────

_FRONT_MATTER_RE = None  # placeholder for potential regex-based parser


def parse_front_matter(text: str, default_id: str = "") -> Tuple[Dict[str, str], str]:
    """Parse a simple YAML-ish front-matter block from a markdown file.

    Format:
        ---
        id: assistant_creator
        name: Assistant Creator
        description: Generates high-quality system prompts...
        ---\n
        <body>

    Returns (metadata, body). Metadata keys are lower-cased; values are
    stripped strings. Files without a front-matter block return ({}, text)
    and the caller can fall back to ``default_id``.
    """
    if not text.startswith("---\n"):
        return {}, text
    # Find the closing delimiter line.
    lines = text.split("\n", 4)
    # lines = ["---", key: value lines..., maybe closing ---]
    body_start = None
    meta_lines: List[str] = []
    rest_lines = text.split("\n")
    # The first line is exactly "---"
    for idx in range(1, len(rest_lines)):
        line = rest_lines[idx]
        if line.strip() == "---":
            body_start = idx + 1
            break
        meta_lines.append(line)
    if body_start is None:
        return {}, text
    meta: Dict[str, str] = {}
    for raw in meta_lines:
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        k = key.strip().lower()
        if k:
            meta[k] = value.strip()
    body = "\n".join(rest_lines[body_start:])
    if body.startswith("\n"):
        body = body[1:]
    meta.setdefault("id", default_id)
    return meta, body


# ─── Default orchestrators ────────────────────────────────────────────────────

def list_default_orchestrator_slugs() -> List[str]:
    """Return slugs (folder names) of all default orchestrator folders.

    Only directories inside defaults/orchestrators/ are considered.
    """
    root = orchestrators_dir()
    if not os.path.isdir(root):
        return []
    result = []
    for name in sorted(os.listdir(root)):
        if name.startswith("."):
            continue
        if os.path.isdir(os.path.join(root, name)):
            result.append(name)
    return result


_METADATA_KEYS = {
    "name", "description", "is_builtin", "sort_order", "max_steps",
    "auto_apply", "tools", "config", "prompt_text", "prompt_file",
}


def _load_orchestrator_new_format(folder: str) -> Optional[Dict[str, Any]]:
    """Load a default orchestrator stored in the new per-folder format.

    Expected layout:
        orchestrator.json      - metadata + config (canonical name, same as
                                 the runtime DATA_DIR/orchestrators layout)
        settings.json          - legacy fallback name for older installations
        system_prompt.md       - system prompt text
        instructions/*.md      - orchestrator-specific instructions
        functions/*.py         - custom Python functions
    """
    settings = read_json(os.path.join(folder, "orchestrator.json"))
    if not isinstance(settings, dict):
        settings = read_json(os.path.join(folder, "settings.json"))
    if not isinstance(settings, dict):
        return None

    slug = os.path.basename(folder.rstrip(os.sep))

    config: Dict[str, Any]
    if isinstance(settings.get("config"), dict):
        config = dict(settings["config"])
    else:
        config = {k: v for k, v in settings.items() if k not in _METADATA_KEYS}

    prompt_text = ""
    prompt_file = os.path.join(folder, "system_prompt.md")
    if os.path.isfile(prompt_file):
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                prompt_text = f.read()
        except OSError:
            prompt_text = ""
    if not prompt_text:
        prompt_text = str(settings.get("prompt_text") or "")

    tools = settings.get("tools")
    if not isinstance(tools, list):
        tools = []

    # Instructions: defaults/orchestrators/<slug>/instructions/*.md
    instructions = []
    instr_dir = os.path.join(folder, "instructions")
    if os.path.isdir(instr_dir):
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
            meta, body = parse_front_matter(raw, default_id=default_id)
            iid = meta.get("id") or default_id
            instructions.append({
                "id": iid,
                "name": meta.get("name") or iid,
                "description": meta.get("description", ""),
                "prompt_text": body,
            })

    # Custom functions: defaults/orchestrators/<slug>/functions/*.py
    functions: Dict[str, str] = {}
    func_dir = os.path.join(folder, "functions")
    if os.path.isdir(func_dir):
        for fname in sorted(os.listdir(func_dir)):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(func_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    functions[fname[:-3]] = f.read()
            except OSError:
                continue

    return {
        "format": "sagaai_orchestrator/v1",
        "slug": slug,
        "name": str(settings.get("name") or slug),
        "description": str(settings.get("description") or ""),
        "prompt_text": prompt_text,
        "config": config,
        "tools": tools,
        "max_steps": int(settings.get("max_steps", 100) or 100),
        "auto_apply": bool(settings.get("auto_apply", True)),
        "is_builtin": bool(settings.get("is_builtin", False)),
        "sort_order": int(settings.get("sort_order", 0) or 0),
        "instructions": instructions,
        "functions": functions,
    }


def _load_orchestrator_old_format(folder: str) -> Optional[Dict[str, Any]]:
    """Load a default orchestrator stored in the legacy export format.

    Expected layout:
        orchestrator.json   - sagaai_orchestrator/v1 export dict (prompt_file allowed)
        system_prompt.md    - optional prompt text next to the JSON
        instructions.json   - optional {id: {...}} dict
        functions/*.py      - optional custom functions
    """
    bundle = read_json(os.path.join(folder, "orchestrator.json"))
    if not isinstance(bundle, dict):
        return None

    slug = os.path.basename(folder.rstrip(os.sep))
    data = dict(bundle)
    data.setdefault("slug", slug)
    data.setdefault("format", "sagaai_orchestrator/v1")

    # Prefer a system_prompt.md sitting next to orchestrator.json.
    prompt_file = os.path.join(folder, "system_prompt.md")
    if os.path.isfile(prompt_file):
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                data["prompt_text"] = f.read()
        except OSError:
            pass
    elif not (data.get("prompt_text") or "").strip():
        rel_prompt = str(data.get("prompt_file") or "")
        if rel_prompt:
            cand = os.path.join(folder, os.path.basename(rel_prompt))
            if os.path.isfile(cand):
                try:
                    with open(cand, "r", encoding="utf-8") as f:
                        data["prompt_text"] = f.read()
                except OSError:
                    pass

    # Instructions from instructions.json if present.
    if "instructions" not in data or not isinstance(data["instructions"], list):
        instructions = []
        instr_path = os.path.join(folder, "instructions.json")
        instr_data = read_json(instr_path, {})
        if isinstance(instr_data, dict):
            for iid, inst in instr_data.items():
                if not isinstance(inst, dict):
                    continue
                instructions.append({
                    "id": str(iid),
                    "name": str(inst.get("name") or iid),
                    "description": str(inst.get("description") or ""),
                    "prompt_text": str(inst.get("prompt_text") or ""),
                })
        data["instructions"] = instructions

    # Functions from functions/*.py if present.
    if "functions" not in data or not isinstance(data["functions"], dict):
        functions: Dict[str, str] = {}
        func_dir = os.path.join(folder, "functions")
        if os.path.isdir(func_dir):
            for fname in sorted(os.listdir(func_dir)):
                if not fname.endswith(".py"):
                    continue
                try:
                    with open(os.path.join(func_dir, fname), "r", encoding="utf-8") as f:
                        functions[fname[:-3]] = f.read()
                except OSError:
                    continue
        data["functions"] = functions

    data.setdefault("config", {})
    data.setdefault("tools", [])
    data.setdefault("max_steps", 100)
    data.setdefault("auto_apply", True)
    data.setdefault("is_builtin", False)
    data.setdefault("sort_order", 0)
    return data


def load_default_orchestrator(slug: str) -> Optional[Dict[str, Any]]:
    """Load a default orchestrator folder and return an import-compatible dict.

    Supports both the new per-folder format (settings.json) and the legacy
    export format (orchestrator.json). Returns None when the folder is
    missing or unreadable.
    """
    folder = os.path.join(orchestrators_dir(), slug)
    if not os.path.isdir(folder):
        return None
    data = _load_orchestrator_new_format(folder)
    if data is None:
        data = _load_orchestrator_old_format(folder)
    if data is None:
        return None
    data.setdefault("slug", slug)
    return data


# ─── Default assistants ───────────────────────────────────────────────────────

def list_default_assistant_folders() -> List[str]:
    """Return folder names of all default assistants."""
    root = assistants_dir()
    if not os.path.isdir(root):
        return []
    result = []
    for name in sorted(os.listdir(root)):
        if name.startswith("."):
            continue
        if os.path.isdir(os.path.join(root, name)):
            result.append(name)
    return result


_ASSISTANT_METADATA_KEYS = {
    "name", "description", "service", "model", "temperature",
    "tools", "max_tool_calls", "max_tokens",
}


def load_default_rag_base(slug: str) -> Optional[Dict[str, Any]]:
    """Return the full manifest dict of a default RAG base, or None.

    The data is read from defaults/rag_bases/<slug>/manifest.json.
    """
    folder = os.path.join(rag_bases_dir(), slug)
    path = os.path.join(folder, "manifest.json")
    data = read_json(path)
    return data if isinstance(data, dict) else None


def load_default_assistant(folder_name: str) -> Optional[Dict[str, Any]]:
    """Load a default assistant folder.

    Expected layout:
        manifest.json    - name/description/service/model/temperature/tools/...
                          (canonical name, same as DATA_DIR/assistants/<slug>/)
        settings.json    - legacy fallback name for older installations
        prompt.md        - system prompt text
        files/           - optional attachment files (text)

    Returns None when the folder is missing or invalid.
    """
    folder = os.path.join(assistants_dir(), folder_name)
    if not os.path.isdir(folder):
        return None
    settings = read_json(os.path.join(folder, "manifest.json"))
    if not isinstance(settings, dict):
        settings = read_json(os.path.join(folder, "settings.json"))
    if not isinstance(settings, dict):
        return None

    prompt_file = os.path.join(folder, "prompt.md")
    prompt_text = ""
    if os.path.isfile(prompt_file):
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                prompt_text = f.read()
        except OSError:
            prompt_text = ""
    if not prompt_text:
        prompt_text = str(settings.get("prompt_text") or "")
    if not prompt_text.strip():
        return None

    files: Dict[str, str] = {}
    files_dir = os.path.join(folder, "files")
    if os.path.isdir(files_dir):
        for fname in sorted(os.listdir(files_dir)):
            fpath = os.path.join(files_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    files[fname] = f.read()
            except (OSError, UnicodeDecodeError):
                continue

    name = str(settings.get("name") or folder_name).strip()
    return {
        "name": name,
        "description": str(settings.get("description") or ""),
        "service": str(settings.get("service") or ""),
        "model": str(settings.get("model") or ""),
        "temperature": settings.get("temperature", 0.3),
        "tools": settings.get("tools", []),
        "max_tool_calls": settings.get("max_tool_calls"),
        "max_tokens": settings.get("max_tokens"),
        "reasoning_effort": settings.get("reasoning_effort"),
        "web_search_context_size": settings.get("web_search_context_size"),
        "web_search_allowed_domains": list(settings.get("web_search_allowed_domains") or []),
        "rag_bases": list(settings.get("rag_bases") or []),
        "prompt_text": prompt_text,
        "files": files,
    }


# ─── Global settings ──────────────────────────────────────────────────────────

def load_global_settings() -> Dict[str, Any]:
    """Return the contents of defaults/settings/global.json (empty dict if missing)."""
    path = os.path.join(settings_dir(), "global.json")
    data = read_json(path, {})
    return data if isinstance(data, dict) else {}
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
