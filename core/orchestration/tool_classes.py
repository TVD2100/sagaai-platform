# -*- coding: utf-8 -*-
"""core.orchestration.tool_classes - what a tool call DOES, not what it is called.

A resumed child must not redo work that already landed on disk. To know that,
the platform has to answer one question about a tool name: does calling it
CHANGE the project?

The answer is a fixed property of the tool, not of the call: ``read_file``
never changes anything and ``propose_file`` always does, whatever arguments
they receive. Keeping the two sets in one place means a new tool is classified
once, and the checkpoint writer, a resume hint and the duplicate-call guard
all read the same answer instead of three drifting copies.

This module deliberately depends on nothing but the standard library: it is
imported by the transcript sink, which is called from inside a running child,
and a heavy import graph there would be paid on every step.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Set

#: Tools that only observe. A failure is worth retrying and a success changes
#: nothing, so a resumed run may call them freely.
READ_TOOLS: Set[str] = {
    "read_file", "list_files", "scan_folder", "assess_workspace",
    "build_project_map", "read_doc", "current_workspace", "verify_file",
    "search_in_files", "show_history", "list_snapshots", "list_thread_files",
    "read_thread_file", "read_document", "web_search", "url_fetch",
    "rag_search", "rag_get_chunks", "list_rag_bases", "analyze_image",
    "list_skills_library", "get_skill_folder", "get_skill_prompt",
    "get_skill_file", "task_state_read", "get_history_index",
    "get_history_messages", "list_recent_workspaces", "list_instructions",
    "get_instruction", "list_assistants", "get_assistant_by_id",
    "list_orchestrators", "get_orchestrator", "list_connections",
    "get_connection", "list_connector_services", "get_connection_memory",
    "get_personal_memory", "list_personal_memory", "subagent_status",
    "subagent_result", "read_subagent_state", "read_subagent_thread",
}

#: Tools that change the project or the platform. Re-running one blindly is
#: how a resume destroys the work it was meant to continue.
MUTATING_TOOLS: Set[str] = {
    "propose_file", "apply_patch", "create_backup", "restore_backup",
    "set_workspace", "set_target_file", "write_doc", "write_project_map",
    "snapshot_all", "restore_all", "task_state_init", "task_state_update",
    "task_state_mark_step", "task_state_clear", "update_assistant_by_id",
    "create_assistant_for_task", "save_orchestrator_function",
    "save_orchestrator_instruction", "delete_orchestrator_function",
    "delete_orchestrator_instruction", "update_connection",
    "create_connection", "delete_connection", "set_personal_memory",
    "set_connection_memory", "generate_image",
}

#: Argument names that identify the target of a mutation, in priority order.
#: ``path`` first because it is what the file tools take; the rest cover the
#: tools that write by name instead (a doc, a function, a memory scope).
_TARGET_ARGS = ("path", "file_path", "doc", "name")


class _CallBlock:
    """One parsed tool call: its name and its arguments."""

    __slots__ = ("tool", "args")

    def __init__(self, tool: str, args: Dict[str, Any]) -> None:
        self.tool = tool
        self.args = args


_FENCE_PATTERN = re.compile(r"```[a-zA-Z0-9_-]*\s*(.*?)```", re.DOTALL)


def is_mutating(tool: str) -> bool:
    """True when calling *tool* can change the project or the platform.

    An unknown tool counts as mutating: a resume that skips an unknown write
    leaves a half-finished file behind, which is worse than re-reading a file
    that did not need it.
    """
    name = str(tool or "").strip()
    if not name:
        return False
    if name in READ_TOOLS:
        return False
    return True


def parse_calls(text: str) -> List[_CallBlock]:
    """Extract the tool calls of one assistant message.

    Only fenced JSON blocks that carry a ``"tool"`` key and an object of
    arguments count. Everything else in a reply - prose, code samples, a
    half-written block - is ignored, so a message that merely TALKS about a
    tool never looks like a call to it.
    """
    calls: List[_CallBlock] = []
    for body in _FENCE_PATTERN.findall(str(text or "")):
        candidate = body.strip()
        if '"tool"' not in candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        tool = str(parsed.get("tool") or "").strip()
        if not tool:
            continue
        args = parsed.get("args")
        if not isinstance(args, dict):
            args = {}
        calls.append(_CallBlock(tool, args))
    return calls


def _target_of(args: Dict[str, Any]) -> str:
    """Return the path or name a mutating call acts on, or an empty string."""
    for key in _TARGET_ARGS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def written_targets(messages: Iterable[Any]) -> List[str]:
    """Return the targets every MUTATING call in *messages* touched.

    Sorted and unique: a resume hint says "these files already exist", and an
    order that depends on the conversation would only add noise. A call with
    no identifiable target is still counted - as the tool name - because "we
    ran snapshot_all" is a fact a resumed child must not repeat blindly.
    """
    found: Set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") != "assistant":
            continue
        for call in parse_calls(str(message.get("content") or "")):
            if not is_mutating(call.tool):
                continue
            found.add(_target_of(call.args) or call.tool)
    return sorted(found)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0