"""The per-thread task-state journal."""
from __future__ import annotations

from typing import Any, Dict, Optional

from .deps import ToolMixin


class TaskStateTools(ToolMixin):
    """The per-thread task-state journal."""

    def task_state_init(self, task: str, architecture: str = "", plan: str = "") -> Dict[str, Any]:
        """Start a new task in this thread's task-state journal.

        Archives the previous Active Task into the journal's Task History
        section, so a new task in the same thread extends the SAME file.
        The journal file is never deleted. Also allocates a numbered
        per-task working folder and records its plain path in the
        ``- task_dir:`` meta line.
        """
        try:
            from dev_agent import task_state as ts
            return ts.archive_and_start_task(
                task, architecture=architecture or "", plan=plan or ""
            )
        except Exception as e:
            return {"ok": False, "error": "task_state_init failed: " + str(e)}

    def task_state_read(self, compact: bool = False) -> Dict[str, Any]:
        """Read this thread's task-state journal, including task_dir.

        With *compact* True a small working-state digest is returned in the
        ``digest`` key instead of the full content/sections - the cheap way
        back into a task whose chat history was truncated.
        """
        try:
            from dev_agent import task_state as ts
            return ts.read_task_state(compact=bool(compact))
        except Exception as e:
            return {"ok": False, "error": "task_state_read failed: " + str(e)}

    def task_state_update(self, section: str, content: str) -> Dict[str, Any]:
        """Update one section of the journal's Active Task, preserving the others."""
        try:
            from dev_agent import task_state as ts
            return ts.update_task_state_section(section, content)
        except Exception as e:
            return {"ok": False, "error": "task_state_update failed: " + str(e)}

    def task_state_mark_step(self, step_id: str, status: str = "done", verification: Optional[str] = None, result: Optional[str] = None, context: Optional[str] = None) -> Dict[str, Any]:
        """Mark one plan step, refresh Progress and record completion facts.

        `context` is the condensed state the NEXT step needs (what was done,
        decisions taken, what to verify next) so the agent can continue
        correctly even when a large part of the chat history is truncated.
        """
        try:
            from dev_agent import task_state as ts
            return ts.update_plan_step_status(
                step_id,
                status=status,
                verification=verification,
                result=result,
                context=context,
            )
        except Exception as e:
            return {"ok": False, "error": "task_state_mark_step failed: " + str(e)}

    def task_state_clear(self) -> Dict[str, Any]:
        """Archive the completed Active Task into Task History.

        The journal file is never deleted.
        """
        try:
            from dev_agent import task_state as ts
            return ts.clear_task_state()
        except Exception as e:
            return {"ok": False, "error": "task_state_clear failed: " + str(e)}


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
