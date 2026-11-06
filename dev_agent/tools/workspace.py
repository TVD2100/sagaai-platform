"""Recent workspace folders and the empty-state guard."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import config
from .deps import ToolMixin


class WorkspaceTools(ToolMixin):
    """Recent workspace folders and the empty-state guard."""

    def _addresses_memory(self, args: Optional[Dict[str, Any]]) -> bool:
        """Return True when every target of the call points into the working memory."""
        return _addresses_memory(args)

    def list_recent_workspaces(self) -> Dict[str, Any]:
        """Return up to 5 recently used workspace paths (newest first).

        Each entry contains a 1-based index (for the user to pick by number),
        the absolute path, and a short display name (the folder basename).
        Paths that no longer exist on disk are filtered out automatically.

        Call this tool at the start of a new task to offer the user a quick
        selection of their most recent projects instead of typing the full path.

        The history is scoped to THIS orchestrator: other employees' folders
        never appear here, and the workspaces of this orchestrator's own
        saved dialogs are included automatically.
        """
        try:
            from dev_agent.workspace_tools import list_recent_workspaces as _ws_recent
            slug = getattr(self, "_orchestrator_slug", "dev_agent") or "dev_agent"
            return _ws_recent(slug)
        except Exception as e:
            return {"ok": False, "error": str(e)}


WORKSPACE_REQUIRED_TOOLS: set = {
    # core file tools
    "read_file", "list_files", "propose_file", "apply_patch", "verify_file",
    "create_backup", "restore_backup", "show_history",
    # workspace-layer file tools (dispatched by UniversalDevAgent)
    "search_in_files", "scan_folder", "assess_workspace",
    "build_project_map", "write_project_map", "write_doc", "read_doc",
    "snapshot_all", "restore_all", "list_snapshots",
}


_PATH_MODE_TOOLS: set = {"run_test", "run_code"}


_WORKSPACE_GUARD_SUGGESTION = (
    "Ask the user which project to work in first (show "
    "list_recent_workspaces as a numbered menu), then call "
    "set_workspace('<path>') - or set_target_file('<path>') for a single "
    "file - and retry the tool."
)


def _addresses_memory(args: Optional[Dict[str, Any]]) -> bool:
    """True when the call's path arguments point into the working memory.

    Every file tool takes its target as ``path``, ``files``, ``subdir`` or
    ``file_path``; a call whose targets are ALL inside the working-memory root
    needs no project folder.
    """
    if not isinstance(args, dict):
        return False
    targets: List[str] = []
    for key in ("path", "subdir", "file_path"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            targets.append(value)
    files = args.get("files")
    if isinstance(files, str) and files.strip():
        targets.append(files)
    elif isinstance(files, (list, tuple)):
        targets.extend(f for f in files if isinstance(f, str) and f.strip())
    if not targets:
        return False
    try:
        return all(config.is_memory_path(t) for t in targets)
    except Exception:
        return False


def workspace_guard_error(tool_name: str, args: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Return a ``workspace_not_selected`` error when the tool needs a workspace.

    Returns None when the empty state is not active, when the tool does not
    operate on project files, or for run_test/run_code in ``code`` mode
    (allowed: the child process runs with a neutral cwd - see
    ``_subprocess_cwd``). ``path`` mode of those two tools IS blocked: a
    script must live in a real project.
    """
    if not isinstance(tool_name, str):
        return None
    needs_ws = tool_name in WORKSPACE_REQUIRED_TOOLS
    if tool_name in _PATH_MODE_TOOLS:
        needs_ws = bool((args or {}).get("path"))
    if not needs_ws:
        return None
    # Working memory (the employee's own data folder) lives in the installation
    # and never depended on a project: a tool that addresses it is allowed in
    # the empty state, otherwise an employee could not read its own registries
    # before the user picks a folder.
    if _addresses_memory(args):
        return None
    if bool(getattr(config, "WORKSPACE_SELECTED", True)):
        return None
    return {
        "ok": False,
        "workspace_not_selected": True,
        "error": (
            f"Tool '{tool_name}' is blocked: this dialog has no workspace "
            "folder selected yet (empty state). File tools never run "
            "against an arbitrary or neutral folder."
        ),
        "suggestion": _WORKSPACE_GUARD_SUGGESTION,
    }


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
