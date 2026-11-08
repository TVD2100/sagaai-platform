"""
core.marketplace.agent_package - reading an agent package and merging an update.

An agent package is a tarball that holds either an orchestrator folder
(``orchestrator.json``, ``system_prompt.md``, ``instructions/*.md``,
``functions/*.py``) or a helper folder (``manifest.json``, ``prompt.md``,
``files/*``). This module turns the unpacked folder into the payload the
importer consumes and decides which installed values an update may move.

The merge rules are the same for both kinds: a value the user never touched
follows the new package, a value they changed stays theirs. The baseline for
that comparison is the manifest of the installed version, read from the cached
package - without a cache every installed value counts as the user's own,
which is the safe direction (an update then changes nothing rather than
overwriting a deliberate setting).
"""
from __future__ import annotations

import os
from typing import Dict, List

from core.marketplace import store
from core.marketplace.installer import InstallError
from core.naming import apply_meta_names


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


def _read_instructions(folder: str) -> List[dict]:
    """Return the instructions a package folder carries, in file order.

    Each ``instructions/*.md`` file is one instruction: the front matter gives
    its id, name and description, the body is the prompt. A file without a
    body is skipped - an empty instruction would show up as a blank card.
    """
    from core.defaults import parse_front_matter

    instructions: List[dict] = []
    instr_dir = os.path.join(folder, "instructions")
    if not os.path.isdir(instr_dir):
        return instructions
    for fname in sorted(os.listdir(instr_dir)):
        if not fname.endswith(".md"):
            continue
        raw = _read_text(os.path.join(instr_dir, fname))
        if not raw.strip():
            continue
        meta, body = parse_front_matter(raw, default_id=fname[:-3])
        iid = str(meta.get("id") or fname[:-3])
        instructions.append({
            "id": iid,
            "name": str(meta.get("name") or iid),
            "description": str(meta.get("description") or ""),
            "prompt_text": body,
        })
    return instructions


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

    instructions = _read_instructions(folder)

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
        "description": str(meta.get("description") or ""),
        "prompt_text": prompt,
        "config": dict(meta.get("config") or {}),
        "tools": list(meta.get("tools") or []),
        "instructions": instructions,
        "functions": functions,
    }
    # The four name slots travel separately: a package may carry a bare name
    # (a user-defined one), a name mapping, or the explicit translation keys.
    bundle = apply_meta_names(bundle, meta)
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
        "name": "",
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
    # The manifest may carry a bare name, a name mapping or explicit
    # translation keys; all four slots are passed on so the importer stores
    # exactly what the package declared.
    bundle = apply_meta_names(bundle, meta)
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


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
