"""Write barrier for a delegated child against the data folder of its employee.

A child inherits the slug of the employee that launched it, so its
``memory:`` root is that employee's registries and journals. Read access is
harmless - a researcher has to look things up - while a write would mix the
findings of a delegated run into the durable record of the role.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .. import config
from .path_scope import addresses_memory

#: Tools that write data a role keeps across dialogs. A delegated child may
#: read the folder of its employee but never append to these.
ROLE_DATA_WRITE_TOOLS: set = {
    "propose_file", "apply_patch", "write_doc", "write_project_map",
    "create_backup", "restore_backup",
}


def role_data_guard_error(tool_name: str, args: Optional[Dict[str, Any]] = None,
                          core: Any = None) -> Optional[Dict[str, Any]]:
    """Refuse a delegated child a write into the data folder of its employee.

    The flag travels on the executor (``child_mode``), set by the child
    runner, because the tool layer has no other way to tell a child from a
    dialog: the run id lives in the orchestration state, not in the
    dispatcher.

    Args:
        tool_name: the tool being called.
        args: its arguments; path-carrying keys are inspected.
        core: the executor that would run the call.

    Returns:
        A refusal payload, or None when the call may run.
    """
    if not isinstance(tool_name, str) or not bool(getattr(core, "child_mode", False)):
        return None
    if tool_name not in ROLE_DATA_WRITE_TOOLS:
        return None
    if not addresses_memory(args, config):
        return None
    return {
        "ok": False,
        "error": (
            f"Tool '{tool_name}' may not write into the working-data folder of "
            "the employee that launched this delegated run. A subagent has no "
            "memory levels: return the result (and the files it produced) to "
            "the caller, and the caller records the outcome in its own memory."
        ),
        "error_code": "child_memory_write",
        "suggestion": (
            "Write produced artifacts to the project folder or the dialog "
            "files folder; do not touch registries, journals or cards."
        ),
    }

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
