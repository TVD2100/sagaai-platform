"""Tools of the declared long-term memory of an employee.

The employee declares its memory levels in ``config["memory_profile"]``; the
prompt block rendered from that declaration tells the agent which level to
read at the start of a task and how to append a record. These tools are the
mechanism behind that text: the budget of an ``always`` level is enforced
here, a ``search`` level cannot be read whole at all, and an append is one
call that validates the record against the declared columns.

A tool has no opinion of its own about the folder: the working-data folder
comes from ``core.orchestrator_folders.resolve_data_dir`` - the same resolver
the prompt block uses - so the path an agent reads and the path the prompt
announces can never disagree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .. import config
from .deps import ToolMixin

#: Refusal of a write aimed at the memory of an employee by a delegated child.
_CHILD_MEMORY_REFUSAL = (
    "A delegated subagent has no memory levels and must not write to the "
    "registries, journals or cards of the employee that launched it. Return "
    "the result to the caller; the caller records the outcome in its own "
    "memory."
)


class MemoryTools(ToolMixin):
    """The declared long-term memory: status, read, append, search, rotate."""

    def _memory_context(self):
        """Resolve (profile, folder) for the running employee.

        Returns:
            A ``(memory_profile module, profile dict, folder Path)`` tuple.

        Raises:
            ValueError: when no employee folder can be resolved.
        """
        from core import memory_profile as mp
        from core import orchestrator_folders as of

        slug = (getattr(self, "_orchestrator_slug", "") or "dev_agent").strip()
        workspace = (str(config.PROJECT_ROOT)
                     if getattr(config, "WORKSPACE_SELECTED", True) else "")
        resolved = of.resolve_data_dir(slug, workspace)
        path = str(resolved.get("path") or "")
        if not path:
            raise ValueError(
                f"the working-data folder of '{slug}' cannot be resolved"
            )
        cfg = {}
        try:
            from core.orchestrators import get_orchestrator
            orch = get_orchestrator(slug) or {}
            cfg = orch.get("config", {}) if isinstance(orch, dict) else {}
        except Exception:
            cfg = {}
        return mp, mp.profile_of(cfg), Path(path)

    def _is_child(self) -> bool:
        """True when this executor runs inside a delegated child."""
        return bool(getattr(self, "child_mode", False))

    def memory_status(self) -> Dict[str, Any]:
        """Report the declared memory levels of this employee.

        Shows each level with its read policy, its file state and the number
        of lines it holds, so a reader learns the shape of the memory without
        reading any of it. A declaration error is reported here instead of
        breaking the prompt.
        """
        try:
            mp, profile, folder = self._memory_context()
        except Exception as exc:
            return {"ok": False, "error": f"memory_status failed: {exc}"}
        levels = []
        for level in profile.get("levels") or []:
            try:
                levels.append(mp.level_status(folder, level))
            except Exception as exc:
                levels.append({"id": level.get("id"), "error": str(exc)})
        return {
            "ok": True,
            "folder": str(folder),
            "folder_exists": folder.exists(),
            "levels": levels,
            "level_count": len(levels),
            "subagent": self._is_child(),
        }

    def memory_read(self, level: str, limit: Optional[int] = None) -> Dict[str, Any]:
        """Read one declared level under its budget.

        An ``always`` level returns at most its declared ``read_lines``; an
        ``on_demand`` level returns a larger window. A ``search`` level is
        refused here: it is answered by ``memory_search`` so that a journal
        which grows for years never lands in the context whole.

        Args:
            level: id of a declared level.
            limit: optional smaller line budget.
        """
        try:
            mp, profile, folder = self._memory_context()
            target = mp.get_level(profile, level)
            result = mp.read_level(folder, target, limit=limit)
        except Exception as exc:
            return {"ok": False, "error": f"memory_read failed: {exc}"}
        return result

    def memory_append(self, level: str, row: Optional[Dict[str, Any]] = None,
                      text: str = "") -> Dict[str, Any]:
        """Prepend one record to a declared level in a single call.

        The platform finds the insertion point (right after the table header,
        or after the title of a plain file), so an append never needs a
        read-modify-write cycle and can never lose the rest of the file. The
        record is validated against the declared columns before the write.

        Args:
            level: id of a declared level.
            row: the record of a table level, keyed by the declared columns.
            text: the body of an entry for a level without columns.
        """
        if self._is_child():
            return {"ok": False, "error": _CHILD_MEMORY_REFUSAL,
                    "error_code": "child_memory_write"}
        try:
            mp, profile, folder = self._memory_context()
            target = mp.get_level(profile, level)
            result = mp.append_entry(folder, target, row=row, text=text)
        except Exception as exc:
            return {"ok": False, "error": f"memory_append failed: {exc}"}
        return result

    def memory_search(self, level: str, query: str) -> Dict[str, Any]:
        """Answer a query against a declared level without reading it whole.

        Args:
            level: id of a declared level.
            query: case-insensitive substring to look for.
        """
        try:
            mp, profile, folder = self._memory_context()
            target = mp.get_level(profile, level)
            result = mp.search_level(folder, target, query)
        except Exception as exc:
            return {"ok": False, "error": f"memory_search failed: {exc}"}
        return result

    def memory_rotate(self, level: str) -> Dict[str, Any]:
        """Move the oldest part of a level file into its archive.

        Retention keeps the reading cost flat: without it a journal grows
        forever and every ``always`` level eventually costs more than the task
        it serves.

        Args:
            level: id of a declared level carrying ``max_rows``.
        """
        if self._is_child():
            return {"ok": False, "error": _CHILD_MEMORY_REFUSAL,
                    "error_code": "child_memory_write"}
        try:
            mp, profile, folder = self._memory_context()
            target = mp.get_level(profile, level)
            result = mp.rotate_level(folder, target)
        except Exception as exc:
            return {"ok": False, "error": f"memory_rotate failed: {exc}"}
        return result


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
