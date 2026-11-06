"""The dialog history window used by economy mode."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.prompt_guard import is_tool_result_text, sanitize_tool_result_content

from .deps import ToolMixin


class HistoryTools(ToolMixin):
    """The dialog history window used by economy mode."""

    def set_history(self, history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Store the full agent history for the economy-mode history tools.

        Called automatically by the agent loop before each LLM request.
        Re-indexes messages on demand; existing _index/_category/_summary
        fields are preserved when present.
        """
        self._history = list(history or [])
        return {"ok": True, "count": len(self._history)}

    def _ensure_history_indexed(self) -> None:
        from dev_agent.agent_loop import _classify_message, _index_message
        for i, msg in enumerate(self._history):
            if "_index" not in msg or "_category" not in msg:
                cat = _classify_message(msg)
                _index_message(msg, i, cat)

    def get_history_index(self, start: int = 0, limit: int = 200) -> Dict[str, Any]:
        """Return a compact index of all stored conversation messages.

        Args:
            start: first index to include (0-based).
            limit: maximum number of entries to return (default 200).

        Returns:
            {ok, total, start, limit, count, entries: [{index, role, category, summary}]}
        """
        self._ensure_history_indexed()
        total = len(self._history)
        try:
            start = max(0, int(start))
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            return {"ok": False, "error": "start and limit must be integers."}
        end = min(total, start + limit)
        entries = []
        for i in range(start, end):
            msg = self._history[i]
            entries.append({
                "index": msg.get("_index", i),
                "role": msg.get("role", ""),
                "category": msg.get("_category", ""),
                "summary": (msg.get("_summary", "") or "")[:120],
            })
        return {
            "ok": True,
            "total": total,
            "start": start,
            "limit": limit,
            "count": len(entries),
            "entries": entries,
        }

    def get_history_messages(self, indices: Optional[List[int]] = None) -> Dict[str, Any]:
        """Return full conversation messages by their indices.

        Args:
            indices: list of 0-based message indices. If omitted, returns
                     an error (prefer get_history_index first to see indices).

        Returns:
            {ok, count, messages: [message dicts]}
        """
        if not indices:
            return {"ok": False, "error": "Provide 'indices' as a list of integers, e.g. indices=[0,1,2]. Call get_history_index() first to see available indices."}
        if not isinstance(indices, list):
            return {"ok": False, "error": "'indices' must be a list of integers."}
        try:
            idx_list = [int(i) for i in indices]
        except (TypeError, ValueError):
            return {"ok": False, "error": "'indices' must contain only integers."}

        self._ensure_history_indexed()
        messages = []
        missing = []
        total = len(self._history)
        for i in idx_list:
            i_int = int(i)
            if 0 <= i_int < total:
                msg = dict(self._history[i_int])
                # Strip internal-only fields to keep the payload clean.
                msg.pop("_events", None)
                # Protect tool-result payloads: wrap as data / sanitize.
                content = msg.get("content", "")
                if isinstance(content, str) and is_tool_result_text(content):
                    msg["content"] = sanitize_tool_result_content(content, source="history_tool_result")
                messages.append(msg)
            else:
                missing.append(i_int)
        return {
            "ok": True,
            "count": len(messages),
            "missing": missing,
            "total": total,
            "messages": messages,
        }


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
