# -*- coding: utf-8 -*-
"""
core.skills_library - management of standardized orchestrator skills.

A skill is a set of files in its own subfolder of the top-level skills/ dir.
Each skill is registered in skills/skills.json:
    {"id": str, "name": str, "description": str, "folder": str}

Supported install methods:
  * ZIP archive (a single skill or a repository with a skill subfolder),
  * a local folder,
  * a GitHub URL such as
      https://github.com/owner/repo
      https://github.com/owner/repo/tree/<branch>/<path>
      https://github.com/owner/repo/archive/refs/heads/<branch>.zip

The user can edit metadata only (name, description, folder name); the skill
files themselves are never modified by this subsystem.

Security: ZIP extraction is protected against path traversal, GitHub
downloads are limited to one repository/subfolder, sizes are limited.

NOTE: DATA_DIR is resolved dynamically via sys.modules on every call. This
keeps the module correct even if core.paths is reloaded at runtime (tests
and hot-reload scenarios).
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─── Paths ────────────────────────────────────────────────────────────────

SKILLS_DIR_NAME = "skills"
REGISTRY_FILE = "skills.json"
REMOVED_DEFAULTS_FILE = "removed_defaults.json"

# Ownership and adaptation metadata (see SPEC.md).
PLATFORM_DEVELOPER = "SagaAI"
UNKNOWN_DEVELOPER = "unknown"


class SkillsLibraryError(Exception):
    """Error raised by skills-library operations."""


_SAFE_FOLDER_RE = re.compile(r"[^a-zA-Z0-9_\-]+")
_SKILL_ID_RE = re.compile(r"^[a-f0-9]{8}$")


# Security limits.
MAX_ZIP_SIZE = 200 * 1024 * 1024        # 200 MB
MAX_TOTAL_FILES = 5000                  # max files per skill
MAX_FILE_SIZE = 50 * 1024 * 1024        # 50 MB per file


def _data_dir() -> str:
    """Return the current DATA_DIR from the live core.paths module.

    Looks up sys.modules first so that importlib.reload(core.paths) (used by
    tests) is respected even after this module was imported.
    """
    mod = sys.modules.get("core.paths")
    if mod is None:
        import core.paths as _cp
        return _cp.DATA_DIR
    return mod.DATA_DIR


# ─── Paths and registry ───────────────────────────────────────────────────────

def get_skills_root() -> str:
    """Return the skills library root directory (DATA_DIR/skills)."""
    return os.path.join(_data_dir(), SKILLS_DIR_NAME)


def ensure_skills_root() -> str:
    """Create the skills root directory if needed and return its path."""
    root = get_skills_root()
    os.makedirs(root, exist_ok=True)
    return root


def _registry_path() -> str:
    return os.path.join(get_skills_root(), REGISTRY_FILE)


def _load_registry() -> Dict[str, dict]:
    """Load the skills registry as {skill_id: record}.

    A corrupt or missing file results in an empty registry.
    """
    path = _registry_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        pass
    return {}


def _save_registry(registry: Dict[str, dict]) -> bool:
    """Persist the skills registry as JSON. Returns True on success."""
    try:
        root = ensure_skills_root()
        path = os.path.join(root, REGISTRY_FILE)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _removed_defaults_path() -> str:
    """Return the path of the removed-defaults markers file."""
    return os.path.join(get_skills_root(), REMOVED_DEFAULTS_FILE)


def _load_removed_defaults() -> List[str]:
    """Return the list of removed default-skill source markers.

    Used by ``core.default_imports.ensure_default_skills`` so a default skill
    the user deleted is never auto-imported again.
    """
    path = _removed_defaults_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(x) for x in data if str(x).strip()]
    except Exception:
        pass
    return []


def _save_removed_defaults(markers: List[str]) -> bool:
    """Persist the removed-defaults markers list. Returns True on success."""
    try:
        root = ensure_skills_root()
        path = os.path.join(root, REMOVED_DEFAULTS_FILE)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(set(str(m) for m in markers if str(m).strip())),
                      f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _new_skill_id() -> str:
    """Generate a unique 8-char hex skill id."""
    registry = _load_registry()
    seen = set(registry.keys())
    while True:
        sid = uuid.uuid4().hex[:8]
        if sid not in seen:
            return sid


def _safe_folder_name(name: str, fallback: str = "skill") -> str:
    """Normalize any input into a safe folder name (a-z, 0-9, '_' and '-').

    Empty strings are replaced with *fallback*.
    """
    cleaned = _SAFE_FOLDER_RE.sub("_", (name or "").strip()).strip("_")
    cleaned = re.sub(r"_{2,}", "_", cleaned)[:64].strip("_")
    if not cleaned:
        cleaned = fallback
    return cleaned


def _unique_folder(name: str) -> str:
    """Return a folder name that does not collide with existing subfolders."""
    root = get_skills_root()
    base = _safe_folder_name(name)
    candidate = base
    suffix = 2
    while os.path.exists(os.path.join(root, candidate)):
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


# ─── Registry CRUD ────────────────────────────────────────────────────────────

def _skill_record(sid: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    """Build the public record of one skill incl. owner/adaptation fields.

    Legacy records without ``developer``/``adapted`` are backfilled:
    skills seeded from ``defaults/skills/`` are SagaAI-owned and adapted,
    everything else is ``unknown`` and not adapted.
    """
    source = str(rec.get("source") or "")
    developer = str(rec.get("developer") or "").strip()
    if not developer:
        developer = PLATFORM_DEVELOPER if source.startswith("defaults/") else UNKNOWN_DEVELOPER
    if "adapted" in rec:
        adapted = bool(rec.get("adapted"))
    else:
        adapted = developer == PLATFORM_DEVELOPER
    return {
        "id": sid,
        "name": rec.get("name", sid),
        "description": rec.get("description", ""),
        "folder": rec.get("folder", ""),
        "developer": developer,
        "adapted": bool(adapted),
        "source": source,
    }


def list_skills(adapted_only: bool = False) -> List[Dict[str, Any]]:
    """Return all installed skills sorted by name.

    Each record: {"id", "name", "description", "folder", "developer",
    "adapted", "source"}. With ``adapted_only=True`` only adapted skills
    are returned (used by orchestrator UIs and system-prompt blocks).
    """
    registry = _load_registry()
    result = []
    for sid, rec in registry.items():
        public = _skill_record(sid, rec)
        if adapted_only and not public["adapted"]:
            continue
        result.append(public)
    result.sort(key=lambda x: x["name"].lower())
    return result


def get_skill(skill_id: str) -> Optional[Dict[str, Any]]:
    """Return one registry record or None (incl. developer/adapted fields)."""
    if not _SKILL_ID_RE.match(skill_id or ""):
        return None
    rec = _load_registry().get(skill_id)
    if rec is None:
        return None
    return _skill_record(skill_id, rec)


def skill_exists(skill_id: str) -> bool:
    """True if the skill id is registered."""
    return get_skill(skill_id) is not None


def _skill_dir(skill_id: str, folder: str = "") -> str:
    """Absolute path to the skill folder by its registry folder name or id."""
    safe = _safe_folder_name(folder) if folder else _safe_folder_name(skill_id, fallback=skill_id)
    return os.path.join(get_skills_root(), safe)


def register_skill(name: str, description: str, folder: str) -> str:
    """Register an already existing folder in skills/ and return a new id."""
    if not name or not name.strip():
        raise SkillsLibraryError("Skill name is required.")
    root = ensure_skills_root()
    folder = _safe_folder_name(folder, fallback=_safe_folder_name(name))
    src = os.path.join(root, folder)
    if not os.path.isdir(src):
        raise SkillsLibraryError(f"Folder does not exist: {folder}")
    sid = _new_skill_id()
    registry = _load_registry()
    registry[sid] = {
        "name": name.strip(),
        "description": (description or "").strip(),
        "folder": folder,
        # Registering an existing folder without an external import is the
        # platform's own path, so the skill is owned by SagaAI and adapted.
        "developer": PLATFORM_DEVELOPER,
        "adapted": True,
    }
    if not _save_registry(registry):
        raise SkillsLibraryError("Failed to save skills registry.")
    return sid


def update_skill(skill_id: str, name: Optional[str] = None,
                 description: Optional[str] = None,
                 folder: Optional[str] = None) -> bool:
    """Update metadata of a skill: name, description and/or folder name.

    When the folder is changed the existing directory is renamed on disk.
    Skill files are never modified. Returns True on success.
    """
    if not _SKILL_ID_RE.match(skill_id or ""):
        return False
    registry = _load_registry()
    rec = registry.get(skill_id)
    if rec is None:
        return False

    if folder is not None and folder.strip():
        new_folder = _safe_folder_name(folder, fallback=rec.get("folder", skill_id))
        old_folder = rec.get("folder", skill_id)
        if new_folder != old_folder:
            root = get_skills_root()
            old_path = os.path.join(root, old_folder)
            new_path = os.path.join(root, new_folder)
            if os.path.exists(new_path):
                raise SkillsLibraryError(f"Folder already exists: {new_folder}")
            if os.path.isdir(old_path):
                os.rename(old_path, new_path)
            rec["folder"] = new_folder

    if name is not None and name.strip():
        rec["name"] = name.strip()
    if description is not None:
        rec["description"] = (description or "").strip()

    return _save_registry(registry)


def set_skill_adapted(skill_id: str, adapted: bool = True) -> bool:
    """Set the adaptation status of a skill. This is how DevAgent marks a

    third-party skill as adapted for the SagaAI platform after the
    Skill Developer adaptation is complete. Returns True on success.
    """
    if not _SKILL_ID_RE.match(skill_id or ""):
        return False
    registry = _load_registry()
    rec = registry.get(skill_id)
    if rec is None:
        return False
    source = str(rec.get("source") or "")
    rec["adapted"] = bool(adapted)
    # Keep the owner stable: platform-seeded skills remain SagaAI skills.
    if source.startswith("defaults/"):
        rec["developer"] = PLATFORM_DEVELOPER
    elif not str(rec.get("developer") or "").strip():
        rec["developer"] = UNKNOWN_DEVELOPER
    return _save_registry(registry)


def delete_skill(skill_id: str) -> bool:
    """Delete a skill: registry record and its folder on disk.

    When the deleted skill was seeded from ``defaults/skills/`` its source
    marker is recorded in ``removed_defaults.json`` so the default import
    never resurrects it.
    """
    if not _SKILL_ID_RE.match(skill_id or ""):
        return False
    registry = _load_registry()
    rec = registry.pop(skill_id, None)
    if rec is None:
        return False
    folder = rec.get("folder", "")
    if folder:
        path = os.path.join(get_skills_root(), _safe_folder_name(folder))
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    source = str(rec.get("source") or "").strip()
    if source:
        removed = _load_removed_defaults()
        if source not in removed:
            removed.append(source)
            _save_removed_defaults(removed)
    return _save_registry(registry)


# ─── Skill files ─────────────────────────────────────────────────────────

def get_skill_folder(skill_id: str) -> Optional[str]:
    """Return the absolute path to a skill folder or None."""
    rec = get_skill(skill_id)
    if rec is None:
        return None
    path = _skill_dir(skill_id, rec.get("folder", ""))
    return path if os.path.isdir(path) else None


def list_skill_files(skill_id: str) -> List[str]:
    """Return relative paths of all files inside a skill folder (recursive)."""
    folder = get_skill_folder(skill_id)
    if not folder:
        return []
    result = []
    for root, _dirs, files in os.walk(folder):
        for fname in sorted(files):
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, folder)
            result.append(rel)
    return sorted(result)


def _target_path_within(root: str, rel_path: str) -> Tuple[str, bool]:
    """Validate a relative path and return (abs_path, safe_bool)."""
    root_abs = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(root, rel_path))
    if os.path.commonpath([root_abs, candidate]) != root_abs:
        return candidate, False
    return candidate, True


# ─── Import: ZIP ──────────────────────────────────────────────────────────

def _extract_zip_to_folder(zip_bytes: bytes, dest: str) -> int:
    """Safely extract a ZIP archive into dest. Returns the number of files."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise SkillsLibraryError(f"Invalid ZIP archive: {exc}") from exc

    os.makedirs(dest, exist_ok=True)
    count = 0
    total_size = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        if info.file_size > MAX_FILE_SIZE:
            raise SkillsLibraryError(f"File too large in ZIP: {info.filename}")
        total_size += info.file_size
        if total_size > MAX_ZIP_SIZE:
            raise SkillsLibraryError("ZIP archive exceeds size limit.")
        count += 1
        if count > MAX_TOTAL_FILES:
            raise SkillsLibraryError("ZIP archive contains too many files.")
        target, safe = _target_path_within(dest, info.filename)
        if not safe:
            raise SkillsLibraryError(f"Unsafe path in ZIP: {info.filename}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with archive.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out, length=1024 * 1024)
    return count


def _find_skill_root_in_dir(directory: str) -> str:
    """Locate the subfolder that contains the skill itself.

    If *directory* holds exactly one subfolder and no files (the typical
    GitHub archive wrapper: {repo}-{branch}/), that subfolder is returned;
    otherwise *directory* itself.
    """
    entries = [e for e in os.listdir(directory) if not e.startswith(".")]
    subdirs = [e for e in entries if os.path.isdir(os.path.join(directory, e))]
    files = [e for e in entries if os.path.isfile(os.path.join(directory, e))]
    if not files and len(subdirs) == 1:
        return os.path.join(directory, subdirs[0])
    return directory


def _copy_tree(src: str, dest: str) -> int:
    """Copy the contents of src (without .git/.github etc.) into dest.

    Returns the number of copied files.
    """
    if not os.path.isdir(src):
        raise SkillsLibraryError(f"Source folder not found: {src}")
    os.makedirs(dest, exist_ok=True)
    count = 0
    for root, dirs, files in os.walk(src):
        # Skip repository/archive service directories.
        dirs[:] = [d for d in dirs if d not in (".git", ".github", "__pycache__", ".ipynb_checkpoints")]
        rel_dir = os.path.relpath(root, src)
        for fname in files:
            src_file = os.path.join(root, fname)
            dst_file = os.path.join(dest, rel_dir, fname) if rel_dir != "." else os.path.join(dest, fname)
            target, safe = _target_path_within(dest, os.path.relpath(dst_file, dest))
            if not safe:
                raise SkillsLibraryError(f"Unsafe path during copy: {fname}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(src_file, target)
            count += 1
    return count


def import_skill_from_zip(zip_bytes: bytes, name: Optional[str] = None,
                          description: str = "",
                          subfolder: Optional[str] = None,
                          developer: Optional[str] = None,
                          adapted: Optional[bool] = None) -> Dict[str, Any]:
    """Install a skill from a ZIP archive.

    *zip_bytes* - archive content; *name* - skill name (falls back to the
    folder name); *subfolder* - optional subfolder inside the archive that
    contains the skill (useful for GitHub repo archives); *developer* -
    the skill author (defaults to ``unknown``); *adapted* - whether the
    skill is adapted for SagaAI (defaults to False for external imports).

    Returns {"ok": True, "skill": {...}} or raises SkillsLibraryError.
    """
    root = ensure_skills_root()
    tmp_dir = os.path.join(root, f".tmp_import_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        _extract_zip_to_folder(zip_bytes, tmp_dir)
        src = _find_skill_root_in_dir(tmp_dir)
        if subfolder:
            candidate = os.path.join(src, subfolder)
            if not os.path.isdir(candidate):
                raise SkillsLibraryError(f"Subfolder not found in ZIP: {subfolder}")
            src = candidate
        return _install_from_folder(src, root, name=name, description=description,
                                    developer=developer, adapted=adapted)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _install_from_folder(src: str, root: str, name: Optional[str],
                         description: str, developer: Optional[str] = None,
                         adapted: Optional[bool] = None) -> Dict[str, Any]:
    """Copy src into a new skill folder and register it.

    ``developer``/``adapted`` describe the skill's ownership and SagaAI
    compatibility status (see SPEC.md). When omitted the defaults are
    ``unknown`` / not adapted, i.e. a skill imported from an external source.
    """
    folder = _unique_folder(name or Path(src).name or "skill")
    dest = os.path.join(root, folder)
    count = _copy_tree(src, dest)
    if count == 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise SkillsLibraryError("No files found in skill source.")
    skill_name = (name or "").strip() or Path(src).name.replace("_", " ").title() or folder
    developer = (developer or "").strip() or UNKNOWN_DEVELOPER
    adapted = bool(adapted)
    sid = _new_skill_id()
    registry = _load_registry()
    registry[sid] = {
        "name": skill_name,
        "description": (description or "").strip(),
        "folder": folder,
        "developer": developer,
        "adapted": adapted,
    }
    if not _save_registry(registry):
        shutil.rmtree(dest, ignore_errors=True)
        raise SkillsLibraryError("Failed to save skills registry.")
    return {"ok": True, "skill": {"id": sid, "name": skill_name,
                                   "description": (description or "").strip(),
                                   "folder": folder,
                                   "developer": developer,
                                   "adapted": adapted}, "files": count}


# ─── Import: local folder ─────────────────────────────────────────────────────

def import_skill_from_folder(folder_path: str, name: Optional[str] = None,
                             description: str = "",
                             developer: Optional[str] = None,
                             adapted: Optional[bool] = None) -> Dict[str, Any]:
    """Install a skill from a local folder.

    The folder is copied to skills/<folder>. *developer* defaults to
    ``unknown`` and *adapted* to False (an external import); pass explicit
    values when the folder is known to be a platform/adapted skill.
    Returns {"ok": True, "skill": ...}.
    """
    if not folder_path or not os.path.isdir(folder_path):
        raise SkillsLibraryError(f"Folder does not exist: {folder_path}")
    root = ensure_skills_root()
    src = os.path.abspath(folder_path)
    fallback = os.path.basename(src.rstrip("/\\")) or "skill"
    return _install_from_folder(src, root, name=name, description=description,
                                developer=developer, adapted=adapted)


# ─── Import: GitHub ───────────────────────────────────────────────────────

def parse_github_url(url: str) -> Dict[str, str]:
    """Parse a GitHub URL into {owner, repo, ref, path}.

    Supported forms:
      https://github.com/owner/repo
      https://github.com/owner/repo/tree/<ref>/<path>
      https://github.com/owner/repo/tree/<ref>
    Returns {} for unsupported URLs.
    """
    if not url:
        return {}
    m = re.match(
        r"https?://(?:www\.)?github\.com/([^/]+)/([^/#?]+)"
        r"(?:/(tree)/[^/]+(?:/(.*?))?)?(?:[?#].*)?$",
        url.strip(),
    )
    if not m:
        return {}
    owner, repo, _, tail = m.group(1), m.group(2), m.group(3), m.group(4) or ""
    return {"owner": owner, "repo": repo, "ref": "main", "path": tail.strip("/")}


def import_skill_from_github(url: str, name: Optional[str] = None,
                             description: str = "",
                             developer: Optional[str] = None,
                             adapted: Optional[bool] = None) -> Dict[str, Any]:
    """Download and install a skill from GitHub.

    Supports repository URLs, specific folder URLs via /tree/..., and direct
    ZIP-archive links. ``developer`` defaults to the GitHub repository owner
    name; pass an explicit value to override it. ``adapted`` defaults to
    False for external sources. Returns {"ok": True, "skill": ...} or
    raises SkillsLibraryError.
    """
    import requests

    url = (url or "").strip()
    if not url:
        raise SkillsLibraryError("GitHub URL is required.")

    # Direct link to a ZIP archive: the URL is the only source of ownership,
    # so an explicit owner keeps a meaningful value for the registry.
    if url.endswith(".zip"):
        resp = requests.get(url, timeout=60)
        if resp.status_code != 200:
            raise SkillsLibraryError(f"GitHub download failed: HTTP {resp.status_code}")
        return import_skill_from_zip(resp.content, name=name, description=description,
                                     developer=developer, adapted=adapted)

    parsed = parse_github_url(url)
    if not parsed:
        raise SkillsLibraryError("Unsupported GitHub URL.")
    owner, repo, ref, path = parsed["owner"], parsed["repo"], parsed["ref"], parsed["path"]
    developer_final = (developer or "").strip() or owner

    zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{ref}"
    try:
        resp = requests.get(zip_url, timeout=120)
        resp.raise_for_status()
    except Exception as exc:
        raise SkillsLibraryError(f"Failed to download GitHub repository: {exc}") from exc

    if len(resp.content) > MAX_ZIP_SIZE:
        raise SkillsLibraryError("GitHub repository archive exceeds size limit.")

    return import_skill_from_zip(resp.content, name=name or (path or repo),
                                 description=description, subfolder=path or None,
                                 developer=developer_final, adapted=adapted)


# ─── Metadata for orchestrator system prompts ───────────────────────────────

def get_enabled_skills_metadata(skill_ids: List[str]) -> List[Dict[str, Any]]:
    """Return metadata of the given skill ids (adapted records only).

    Non-adapted skills must not leak into orchestrator system prompts: only
    skills marked as adapted for SagaAI are exposed in the "Available skills"
    block. The DevAgent tool ``list_skills_library`` still uses list_skills()
    directly and sees every skill.
    """
    if not skill_ids:
        return []
    result = []
    for sid in skill_ids:
        rec = get_skill(sid)
        if rec and rec.get("adapted"):
            result.append(rec)
    return result


def build_skills_metadata_text(skill_ids: List[str], lang: str = "en") -> str:
    """Build a Markdown block listing skills for a system prompt.

    Returns an empty string when no skills are enabled. Skill ids missing
    from the registry are ignored. For every enabled skill the block also
    includes its absolute folder path (resolved from the skills library root)
    and the exact tools the orchestrator should use to invoke it.
    """
    metas = get_enabled_skills_metadata(skill_ids)
    if not metas:
        return ""
    lines = ["## Available skills", "", "The following standardized skills are installed and enabled for you:", ""]
    for m in metas:
        sid = m.get("id", "")
        desc = m.get("description", "").strip()
        folder_rel = m.get("folder", "")
        folder_abs = ""
        try:
            folder_abs = get_skill_folder(sid)
        except Exception:
            folder_abs = None
        if not folder_abs:
            folder_abs = os.path.join(get_skills_root(), folder_rel) if folder_rel else ""
        lines.append(f"- **{m.get('name', sid)}** (id: `{sid}`, folder: `{folder_rel}`)")
        if folder_abs:
            lines.append(f"  folder_path: `{folder_abs}`")
        if desc:
            lines.append(f"  {desc}")
    lines.append("")
    lines.append("To invoke a skill call `get_skill_prompt(<skill_id>)` to load its instructions, "
                 "`get_skill_folder(<skill_id>)` for the folder path and file list, and "
                 "`get_skill_file(<skill_id>, <filename>)` for individual files. "
                 "`list_skills_library()` lists all installed skills. "
                 "Skill folders are absolute paths on disk; you may also read files inside them with read_file.")
    return "\n".join(lines)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
