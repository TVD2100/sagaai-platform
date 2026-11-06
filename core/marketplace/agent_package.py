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
from typing import Dict, List, Optional

from core.marketplace import store
from core.marketplace.installer import InstallError


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


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
