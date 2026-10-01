"""
core.recent_workspaces - persistent history of recently used DevAgent workspaces.

Workspace selection is a common friction point: when starting a new task the user
has to type/paste the full absolute path again, even if they worked in that
project a couple of hours ago.

The history is stored in the SQLite ConfigKV table as a dict keyed by
orchestrator slug, so each orchestrator owns its own shelf::

    {"dev_agent": ["/path/a"], "teacher_assistant": ["/path/b"]}

The legacy flat-list format (a plain JSON list) is read as the dev_agent
scope, so existing installations keep their history. The dict survives app
restarts and "New dialog" resets.

Read-side filtering keeps the suggestions usable and safe:
  * non-existent paths are dropped;
  * the neutral empty-state root is never suggested (it is a technical
    placeholder, not a chosen project);
  * scopes other than dev_agent additionally hide the platform's own folders
    (the SagaAI install root and everything inside it - apps/, .dev_agent/,
    history/, ...): those belong to the developer workflow and would confuse
    other employees.

When a scope has little explicit history, the workspaces of the scope's OWN
dialogs (threads in devagent.db) are used as additional candidates, newest
first. A dialog of teacher_assistant can therefore never surface a folder
that only DevAgent dialogs ever used.

Public API:
    get_recent_workspaces(slug=None) -> list[str]
    add_recent_workspace(path, slug=None) -> None
    clear_recent_workspaces(slug=None) -> None
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from storage.repository import repo_load_config, repo_save_config

# ConfigKV key under which the scoped recent-workspaces dict is stored.
RECENT_WORKSPACES_KEY = "recent_workspaces"

# Maximum number of workspace paths we remember per orchestrator scope.
MAX_RECENT_WORKSPACES = 5

# Default scope: the built-in developer. Legacy (unscoped) API calls map here.
SCOPE_DEV_AGENT = "dev_agent"


def _norm_scope(slug: Optional[str]) -> str:
    """Normalize an orchestrator slug; None/empty means the dev_agent scope."""
    return (str(slug or "").strip()) or SCOPE_DEV_AGENT


def _normalise_path(path: str) -> str:
    """Expand user, resolve, and return the absolute path as a string."""
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser().resolve())
    except (OSError, RuntimeError):
        # Path may not exist yet (create-new-project flow). Return abspath as-is.
        return str(Path(raw).expanduser().absolute())


def _load_scopes() -> Dict[str, List[str]]:
    """Load the scoped history from ConfigKV (legacy list -> dev_agent scope)."""
    try:
        cfg = repo_load_config()
        raw = cfg.get(RECENT_WORKSPACES_KEY, {})
    except Exception:
        return {}

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = {}

    if isinstance(raw, list):
        # Legacy flat format: implicitly the dev_agent scope.
        return {SCOPE_DEV_AGENT: [x for x in raw if isinstance(x, str)]}

    if not isinstance(raw, dict):
        return {}

    scopes: Dict[str, List[str]] = {}
    for key, value in raw.items():
        if isinstance(value, list):
            scopes[str(key)] = [x for x in value if isinstance(x, str)]
    return scopes


def _save_scopes(scopes: Dict[str, List[str]]) -> None:
    """Persist the scoped history into ConfigKV (best effort)."""
    try:
        cfg = repo_load_config()
        cfg[RECENT_WORKSPACES_KEY] = scopes
        repo_save_config(cfg)
    except Exception:
        # Persistence failure must never break workspace switching.
        pass


def _is_hidden(path: str, scope: str) -> bool:
    """True when this path must NOT be suggested inside this scope.

    Rules:
      * the neutral empty-state root is hidden in every scope - it is a
        technical placeholder, not a chosen project;
      * scopes other than dev_agent hide the platform's own folders (the
        SagaAI install root and anything inside it), because those belong
        to the developer workflow of DevAgent itself.
    """
    try:
        from dev_agent import config as dagent_config
        resolved = Path(path).resolve()
        try:
            neutral = Path(dagent_config.NEUTRAL_ROOT).resolve()
        except Exception:
            neutral = None
        if neutral is not None and resolved == neutral:
            return True
        if scope != SCOPE_DEV_AGENT:
            install = Path(dagent_config.INSTALL_ROOT).resolve()
            if resolved == install or install in resolved.parents:
                return True
    except Exception:
        # A config failure must never hide legitimate entries.
        return False
    return False


def _seed_from_threads(scope: str) -> List[str]:
    """Return workspace paths of the scope's OWN dialogs, newest first.

    Read-time seeding: the user's explicit history stays the primary source,
    but dialogs are a reliable record of where this orchestrator actually
    worked. Threads of OTHER orchestrators are never consulted, so a scope
    can never inherit a neighbouring scope's folders.
    """
    seeds: List[str] = []
    try:
        from storage.repository_devagent import repo_devagent_list_threads
        for meta in repo_devagent_list_threads(scope):
            workspace = str(meta.get("workspace") or "").strip()
            if workspace:
                seeds.append(workspace)
    except Exception:
        return []
    return seeds


def get_recent_workspaces(slug: Optional[str] = None) -> List[str]:
    """Return up to MAX_RECENT_WORKSPACES recently used workspace paths.

    The scope is selected by *slug* (default: dev_agent). The list is sorted
    newest-first: explicitly recorded paths come first, then the workspaces
    of the scope's own dialogs. Paths that no longer exist on disk, the
    neutral empty-state root and - for non-dev_agent scopes - the platform's
    own folders are filtered out, so no menu ever suggests dead or foreign
    locations.
    """
    scope = _norm_scope(slug)
    stored = _load_scopes().get(scope, [])
    candidates: List[str] = [item for item in stored if isinstance(item, str)]
    candidates.extend(_seed_from_threads(scope))

    result: List[str] = []
    for item in candidates:
        norm = _normalise_path(item)
        if not norm or norm in result:
            continue
        if not Path(norm).exists():
            continue
        if _is_hidden(norm, scope):
            continue
        result.append(norm)
        if len(result) >= MAX_RECENT_WORKSPACES:
            break
    return result


def add_recent_workspace(path: str, slug: Optional[str] = None) -> None:
    """Add a workspace path to the top of the recent list of one scope.

    Duplicates are removed, hidden paths (the neutral root, or the platform
    folders for non-dev_agent scopes) are ignored, the list is capped at
    MAX_RECENT_WORKSPACES per scope, and the result is persisted in ConfigKV.
    """
    norm = _normalise_path(path)
    if not norm:
        return
    scope = _norm_scope(slug)
    if _is_hidden(norm, scope):
        return  # a placeholder / platform path is never worth remembering

    scopes = _load_scopes()
    current: List[str] = []
    for item in scopes.get(scope, []):
        norm_item = _normalise_path(item)
        if norm_item and norm_item != norm and norm_item not in current:
            current.append(norm_item)
    current.insert(0, norm)
    scopes[scope] = current[:MAX_RECENT_WORKSPACES]
    _save_scopes(scopes)


def clear_recent_workspaces(slug: Optional[str] = None) -> None:
    """Remove the recent-workspaces history.

    Without ``slug`` the ENTIRE history (every scope) is removed - the
    legacy semantics. With a slug only that orchestrator's list is cleared.
    """
    try:
        if slug:
            scopes = _load_scopes()
            scopes.pop(_norm_scope(slug), None)
            _save_scopes(scopes)
        else:
            cfg = repo_load_config()
            cfg.pop(RECENT_WORKSPACES_KEY, None)
            repo_save_config(cfg)
    except Exception:
        pass
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
