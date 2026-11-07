"""Recent workspace folders and the empty-state guard.

The guard answers one question: may this tool call run before the user has
chosen a project folder? File tools must not - they would touch an arbitrary
folder - while the working memory and the dialog's own files folder never
depended on a project and stay reachable.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .. import config
from .deps import ToolMixin
from .path_scope import addresses_dialog_files, addresses_memory


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
    """True when the call's path arguments point into the working memory."""
    return addresses_memory(args, config)


def _addresses_dialog_files(args: Optional[Dict[str, Any]]) -> bool:
    """True when the call's path arguments point into the dialog files folder."""
    return addresses_dialog_files(args, config)


def workspace_guard_error(tool_name: str, args: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Return a ``workspace_not_selected`` error when the tool needs a workspace.

    Returns None when the empty state is not active, when the tool does not
    operate on project files, or for run_test/run_code in ``code`` mode
    (allowed: the child runs with a neutral cwd). ``path`` mode of those two
    tools IS blocked: a script must live in a real project.
    """
    if not isinstance(tool_name, str):
        return None
    needs_ws = tool_name in WORKSPACE_REQUIRED_TOOLS
    if tool_name in _PATH_MODE_TOOLS:
        needs_ws = bool((args or {}).get("path"))
    if not needs_ws:
        return None
    # The working memory and the dialog files folder both live in the
    # installation and never depended on a project: a tool that addresses one
    # of them is allowed in the empty state, otherwise an employee could not
    # read its own registries before the user picks a folder.
    if _addresses_memory(args) or _addresses_dialog_files(args):
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
