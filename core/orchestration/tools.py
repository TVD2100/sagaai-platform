# -*- coding: utf-8 -*-
"""core.orchestration.tools - the delegation tools exposed to the model.

Two tools, one contract:

- spawn_agent launches a CHILD ORCHESTRATOR: a full agent with its own
  tools, its own thread and its own journal. Blocking in v1 - it returns the
  child result once the child finishes.
- invoke_assistant makes ONE model call to an existing assistant (or an
  ephemeral prompt): no tools, no RAG bases, no agent loop.

Rights are enforced here, next to the arguments, so the same refusal is
produced whether the tool is reached through the loop branch or through
dispatch. The rule set is small and explicit:

- spawn_agent is ALWAYS callable: self-delegation needs no configuration, so
  the orchestrator's own slug is allowed by definition. The allowlist
  narrows the circle of OTHER agents only.
- an empty allowlist therefore never produces an empty ``allowed`` list;
  it produces ``allowed: [self]``, ``self_only: true`` and a hint, so the
  model does not spend steps probing for other names.
- invoke_assistant is registered only when the allowlist is non-empty:
  there are no assistants by default, unlike the right to call yourself.

The IMPLEMENTATION lives in the ``tools_*`` modules next to this one, split
by topic: rights, run records, spawn, status, the journal reader, control,
the question protocol, the wait and the catalog. This module is the single
place the names are BOUND: the agent loop imports them from here at call
time, and a test that replaces ``tools.spawn_agent`` must keep working -
so nothing outside may import the implementation modules directly.
"""

from __future__ import annotations

import time  # noqa: F401 - the facade owns the clock; tools_clock reads it

from core.orchestration.tools_catalog import (
    answer_child_tool_entry,
    ask_parent_tool_entry,
    cancel_subagent_tool_entry,
    invoke_tool_entry,
    read_subagent_state_tool_entry,
    read_subagent_thread_tool_entry,
    resume_subagent_tool_entry,
    sleep_tool_entry,
    spawn_tool_entry,
    subagent_result_tool_entry,
    subagent_status_tool_entry,
    verify_result_tool_entry,
)
from core.orchestration.tools_constants import (
    MAX_SLEEP_SECONDS,
    MAX_STATE_CHARS,
    MAX_STATUS_WAIT_SECONDS,
)
from core.orchestration.tools_control import (
    cancel_subagent,
    resume_subagent,
    verify_result,
)
from core.orchestration.tools_dialog import (
    answer_child,
    ask_parent,
    read_dialog_limits,
)
from core.orchestration.tools_journal import (
    read_subagent_thread,
)
from core.orchestration.tools_notes import (
    _attach_note,
    _journal_note,
    _note_for_payload,
    fence_question_payload,
    run_note,
)
from core.orchestration.tools_records import (
    _child_thread_row_missing,
    _confine_run,
    _is_live_result,
    _load_run_record,
    _resolve_run_root,
)
from core.orchestration.tools_rights import (
    _allowlist_context,
    _available_agents,
    _validate_brief,
    check_agent_allowed,
    check_assistant_allowed,
)
from core.orchestration.tools_spawn import (
    invoke_assistant,
    spawn_agent,
)
from core.orchestration.tools_status import (
    read_subagent_state,
    subagent_result,
    subagent_status,
)
from core.orchestration.tools_wait import (
    _active_run_ids,
    _terminal_run_ids,
    sleep_tool,
)

__all__ = [
    "MAX_SLEEP_SECONDS",
    "MAX_STATE_CHARS",
    "MAX_STATUS_WAIT_SECONDS",
    "_active_run_ids",
    "_allowlist_context",
    "_attach_note",
    "_available_agents",
    "_child_thread_row_missing",
    "_confine_run",
    "_is_live_result",
    "_journal_note",
    "_load_run_record",
    "_note_for_payload",
    "_resolve_run_root",
    "_terminal_run_ids",
    "_validate_brief",
    "answer_child",
    "answer_child_tool_entry",
    "ask_parent",
    "ask_parent_tool_entry",
    "cancel_subagent",
    "cancel_subagent_tool_entry",
    "check_agent_allowed",
    "check_assistant_allowed",
    "fence_question_payload",
    "invoke_assistant",
    "invoke_tool_entry",
    "read_dialog_limits",
    "read_subagent_state",
    "read_subagent_state_tool_entry",
    "read_subagent_thread",
    "read_subagent_thread_tool_entry",
    "resume_subagent",
    "resume_subagent_tool_entry",
    "run_note",
    "sleep_tool",
    "sleep_tool_entry",
    "spawn_agent",
    "spawn_tool_entry",
    "subagent_result",
    "subagent_result_tool_entry",
    "subagent_status",
    "subagent_status_tool_entry",
    "verify_result",
    "verify_result_tool_entry",
]


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
