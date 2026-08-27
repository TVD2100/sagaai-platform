"""
core.recent_workspaces - persistent history of recently used DevAgent workspaces.

Workspace selection is a common friction point: when starting a new task the user
has to type/paste the full absolute path again, even if they worked in that
project a couple of hours ago.

This module stores the last N (default 5) workspace folder paths in the SQLite
ConfigKV table.  The history is global (not per-session), so it survives app
restarts and "New dialog" resets.

Public API:
    get_recent_workspaces() -> list[str]
    add_recent_workspace(path) -> None
    clear_recent_workspaces() -> None
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

from storage.repository import repo_load_config, repo_save_config

# ConfigKV key under which the recent-workspaces JSON list is stored.
RECENT_WORKSPACES_KEY = "recent_workspaces"

# Maximum number of workspace paths we remember.
MAX_RECENT_WORKSPACES = 5


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


def get_recent_workspaces() -> List[str]:
    """Return up to MAX_RECENT_WORKSPACES recently used workspace paths.

    The list is sorted newest-first.  Paths that no longer exist on disk are
    filtered out so the UI never suggests dead folders.
    """
    try:
        cfg = repo_load_config()
        raw = cfg.get(RECENT_WORKSPACES_KEY, [])
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                raw = []
        if not isinstance(raw, list):
            raw = []
    except Exception:
        raw = []

    result: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        norm = _normalise_path(item)
        if not norm:
            continue
        if norm in result:
            continue
        if not Path(norm).exists():
            continue
        result.append(norm)
        if len(result) >= MAX_RECENT_WORKSPACES:
            break
    return result


def add_recent_workspace(path: str) -> None:
    """Add a workspace path to the top of the recent list.

    Duplicates are removed, the list is capped at MAX_RECENT_WORKSPACES,
    and the result is persisted in ConfigKV.
    """
    norm = _normalise_path(path)
    if not norm:
        return

    current = get_recent_workspaces()
    # Remove this path if already present, then prepend it.
    current = [p for p in current if p != norm]
    current.insert(0, norm)
    current = current[:MAX_RECENT_WORKSPACES]

    try:
        cfg = repo_load_config()
        cfg[RECENT_WORKSPACES_KEY] = current
        repo_save_config(cfg)
    except Exception:
        # Persistence failure must never break workspace switching.
        pass


def clear_recent_workspaces() -> None:
    """Remove the recent-workspaces history entirely."""
    try:
        cfg = repo_load_config()
        cfg.pop(RECENT_WORKSPACES_KEY, None)
        repo_save_config(cfg)
    except Exception:
        pass
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
