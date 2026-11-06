# -*- coding: utf-8 -*-
"""core.orchestration.child_text - the launch context of a delegated child.

A child run gets its OWN thread id and its OWN journal file, and it starts
with a CLEAN history: the goal, the brief and a handful of absolute paths are
the only channel it has. The helpers below compose that context and hold no
state - they read no registry and touch no run record - so the runner builds
the launch text without dragging the service along.
"""

from __future__ import annotations

import os
from typing import List, Optional

from core.orchestration.contracts import AgentBrief


def derive_child_thread_id(parent_thread_id: str, run_id: str) -> str:
    """Return the derived thread id of a child run.

    The child gets its OWN thread id and its OWN journal file: ``thread_id``
    is a pointer to the journal path, and inheriting the parent's would mean
    delegated work writing into the parent's Active Task (and
    ``task_state_init`` would archive the parent's working state). The
    workspace folder is inherited - only the journal differs.
    """
    parent = (parent_thread_id or "").strip() or "nothread"
    return parent + "__s" + (run_id or "")[:8]


def thread_files_dir_of(thread_id: Optional[str]) -> str:
    """Return the absolute dialog-uploads folder of a thread, or an empty string.

    The child gets this path in its task text so it can read the parent's
    uploads by an absolute path without probing the filesystem. An empty or
    unknown thread simply yields no line - the child keeps working from its
    own binding.
    """
    tid = str(thread_id or "").strip()
    if not tid:
        return ""
    try:
        from core.threads_devagent import _thread_files_dir
        return os.path.abspath(_thread_files_dir(tid))
    except Exception:  # pragma: no cover - defensive
        return ""


def build_child_task_text(brief: AgentBrief, base_dir: Optional[str],
                          uploads_dir: str = "") -> str:
    """Compose the task text of a child run: goal, brief and absolute paths.

    The child starts with a CLEAN history and its own workspace binding, so
    it cannot inherit the parent's idea of "current folder" or its dialog
    uploads. Everything it may need is stated here: the goal, the brief, the
    input artifacts and two absolute paths - the shared workspace and the
    parent's dialog files folder. Without them the child spends its first
    steps guessing where the files are.

    ``uploads_dir`` arrives RESOLVED rather than as a thread id: the caller
    owns the lookup, so a patched resolver in the caller keeps working.
    """
    task_text = str(brief.goal or "")
    if brief.brief:
        task_text += "\n\n" + str(brief.brief)
    if brief.artifacts_in:
        lines = ["\n\nВходные артефакты:"]
        for name, path in brief.artifacts_in.items():
            lines.append("- " + str(name) + ": " + str(path))
        task_text += "\n".join(lines)

    context_lines: List[str] = []
    # A child is an executor, not a dialog partner. Without these lines it
    # starts with the full agent prompt, where Stage 1 demands a plan and an
    # approval stop - and a child has nobody to approve anything, so it
    # spends its step budget re-presenting a plan nobody will read.
    context_lines.append(
        "- Роль: делегированный исполнитель. Пользователя в этом диалоге нет, "
        "план утверждать некому."
    )
    context_lines.append(
        "- План в ответе не печатай: запиши его в журнал задачи "
        "(task_state_init) и сразу переходи к выполнению."
    )
    context_lines.append(
        "- Завершив работу, ответь итогом и закончи ответ строкой "
        '{"loop_status": "done"}. Значение "awaiting_user" здесь означает '
        "незавершённую работу и останавливает родителя."
    )
    workspace_root = str(base_dir or "").strip()
    if workspace_root:
        context_lines.append("- Рабочая папка: " + os.path.abspath(workspace_root))
    thread_files = str(uploads_dir or "").strip()
    if thread_files:
        context_lines.append(
            "- Файлы диалога родителя: " + thread_files
            + " (передавай эти пути целиком, они абсолютные)"
        )
    if context_lines:
        task_text += ("\n\nКонтекст запуска (абсолютные пути):\n"
                      + "\n".join(context_lines))
    return task_text
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
