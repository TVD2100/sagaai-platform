# -*- coding: utf-8 -*-
"""core.orchestration.transcript_sink - persist a running child as it works.

The runner owns a live ``AgentLoopState`` whose ``history`` grows with every
step. That list is the only copy of the child's conversation, and it dies with
the process. This module drains it into the transcript file and keeps the
index row in step with it, so the work survives a crash, a kill or an OOM:
the parent can read what the child actually did and resume from there.

One sink per child run, driven by the runner:

    sink = TranscriptSink(thread_id=..., parent_thread_id=..., slug=...)
    sink.open()                 # create the index row
    ...
    sink.drain(state)           # after every step: append new messages
    ...
    sink.finish(state)          # final drain + index flush

Three decisions worth stating:

- The index row is created BEFORE the first step. Without it the child is not
  addressable at all: ``read_subagent_thread`` resolves a run to a thread and
  then to a transcript, and a row that does not exist answers "not found"
  instead of "here is what happened".
- The drain cursor is the number of history messages already written, and it
  advances by what actually landed on disk - not by the length of the list. A
  failed write therefore leaves the cursor where it was and the next drain
  retries that message instead of skipping it for the rest of the run.
- The sink is the ONLY writer of its child's counters, so it writes them as
  absolute values. A delta would have to be applied on every flush and would
  multiply the count by the number of flushes.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

#: Kind stored in the index row for a delegated child.
KIND_SUBAGENT = "subagent"

#: How many characters of the task become the child's title.
TITLE_CHARS = 60


def _one_line(text: Any, limit: int = TITLE_CHARS) -> str:
    """Collapse whitespace and cut, for a title that fits one row."""
    flat = " ".join(str(text or "").split())
    return flat[:limit]


def _now() -> str:
    """Current local time in the shape the chat rows use."""
    return datetime.now().isoformat(timespec="seconds")


class TranscriptSink:
    """Write one child run's conversation to its transcript and index.

    Every method is failure-tolerant: a transcript is an aid to recovery, and
    a sink that raises would turn a working run into a failed one. Errors are
    logged and reported through the return values, never propagated.
    """

    def __init__(self, thread_id: str, parent_thread_id: str = "",
                 slug: str = "", display_name: str = "",
                 workspace: Optional[str] = None) -> None:
        self.thread_id = str(thread_id or "").strip()
        self.parent_thread_id = str(parent_thread_id or "").strip()
        self.slug = str(slug or "").strip()
        self.display_name = str(display_name or self.slug or "DevAgent")
        self.workspace = workspace or None
        self.written = 0          # history messages already on disk
        self.messages = 0         # records on disk (the index counter)
        self.bytes_total = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.tokens_cache = 0
        self.row_ready = False
        self.rel_path = ""
        self.error = ""

    # ── lifecycle ──────────────────────────────────────────────────────────

    def open(self, task_text: str = "") -> bool:
        """Create the index row that makes this child addressable.

        The task text becomes the row's title, not a transcript record: the
        loop appends the real user message to ``history`` on its first step,
        and writing a reconstruction here would duplicate it.
        """
        if not self.thread_id:
            return False
        try:
            from dev_agent import transcript as T
            self.rel_path = T._relative_to_history(
                T.transcript_path(self.thread_id, self.parent_thread_id or None))
        except Exception as exc:  # pragma: no cover - path is best effort
            self.error = str(exc)
            self.rel_path = ""
        try:
            from storage.repository_devagent import (
                repo_devagent_create_thread,
                repo_devagent_load_thread_meta,
            )
            if repo_devagent_load_thread_meta(self.thread_id):
                # A retried or resumed run reuses its own row; the transcript
                # path is refreshed below in case the row predates the file.
                self.row_ready = True
            else:
                self.row_ready = repo_devagent_create_thread(
                    self.thread_id,
                    title=_one_line(task_text),
                    orchestrator_slug=self.slug or None,
                    orchestrator_name=self.display_name,
                    workspace=self.workspace,
                    parent_thread_id=self.parent_thread_id or None,
                    kind=KIND_SUBAGENT,
                    transcript_path=self.rel_path or None,
                )
        except Exception as exc:  # pragma: no cover - index is best effort
            self.error = str(exc)
            log.warning("transcript index open failed: %s", exc)
        if self.row_ready and self.rel_path:
            self._flush_index()
        return self.row_ready

    def drain(self, state: Any) -> int:
        """Append every history message not yet written; return how many.

        Called after each step, so the transcript trails the child by at most
        one step. A crash between two steps loses that step and nothing else.
        """
        history = list(getattr(state, "history", None) or [])
        if self.written >= len(history):
            return 0
        fresh = history[self.written:]
        written = self._write(fresh)
        # Advance by what LANDED, not by what was offered: a failed write must
        # be retried by the next drain, not quietly skipped.
        self.written += written
        if written:
            self._flush_index()
        return written

    def checkpoint(self, state: Any = None, status: str = "") -> Dict[str, Any]:
        """Record where the child stands, on a step boundary.

        A checkpoint is written AFTER the drain, so it never describes a
        message that is not on disk yet. It carries what a resume would
        otherwise have to reconstruct by parsing the conversation: the step
        counter, the files the child already wrote, and its own final text.

        Failure-tolerant like every other method here: progress bookkeeping
        must not be able to stop the work it is bookkeeping.
        """
        if not self.thread_id:
            return {"ok": False, "error": "no thread"}
        try:
            from dev_agent import transcript as T
        except Exception as exc:  # pragma: no cover - never raise
            return {"ok": False, "error": str(exc)}
        history = list(getattr(state, "history", None) or [])
        try:
            from core.orchestration.tool_classes import written_targets
            written = written_targets(history)
        except Exception:  # pragma: no cover - classification is best effort
            written = []
        try:
            record = T.build_checkpoint(
                0,
                step=int(getattr(state, "steps", 0) or 0),
                max_steps=int(getattr(state, "max_steps", 0) or 0),
                written=written,
                status=str(status or ""),
                note=str(getattr(state, "task", "") or ""),
                tokens={"in": int(getattr(state, "total_tokens_in", 0) or 0),
                        "out": int(getattr(state, "total_tokens_out", 0) or 0),
                        "cache": int(getattr(state, "total_tokens_cache", 0) or 0)},
            )
            result = T.append_checkpoint(self.thread_id, record, self.messages + 1,
                                         self.parent_thread_id or None)
        except Exception as exc:  # pragma: no cover - a log must not break a run
            self.error = str(exc)
            return {"ok": False, "error": str(exc)}
        if result.get("ok"):
            self.bytes_total += int(result.get("bytes") or 0)
            self._flush_index()
        return result

    def finish(self, state: Any = None, status: str = "",
               error_code: str = "") -> Dict[str, Any]:
        """Final drain and index flush; return a summary of what was stored."""
        if state is not None:
            self.drain(state)
        self._flush_index()
        return {
            "thread_id": self.thread_id,
            "parent_thread_id": self.parent_thread_id,
            "messages": self.messages,
            "bytes": self.bytes_total,
            "transcript_path": self.rel_path,
            "row_ready": self.row_ready,
            "status": status,
            "error_code": error_code,
            "error": self.error,
        }

    # ── internals ──────────────────────────────────────────────────────────

    def _write(self, messages) -> int:
        """Append messages to the file; return how many actually landed."""
        try:
            from dev_agent import transcript as T
        except Exception as exc:  # pragma: no cover
            self.error = str(exc)
            return 0
        written = 0
        for message in messages:
            if not isinstance(message, dict):
                # A non-dict entry is not storable; skipping it must still
                # advance the cursor past it, or every later drain retries it.
                written += 1
                continue
            result = T.append_record(
                T.build_record(message, self.messages + 1),
                self.thread_id, self.parent_thread_id or None)
            if not result.get("ok"):
                self.error = str(result.get("error") or "write failed")
                break
            written += 1
            self.messages += 1
            self.bytes_total += int(result.get("bytes") or 0)
            if not self.rel_path:
                self.rel_path = str(result.get("path") or "")
            tokens = (message.get("_tokens")
                      if isinstance(message.get("_tokens"), dict) else {})
            if tokens:
                self.tokens_in += int(tokens.get("in", 0) or 0)
                self.tokens_out += int(tokens.get("out", 0) or 0)
                self.tokens_cache += int(tokens.get("cache", 0) or 0)
        return written

    def _flush_index(self) -> None:
        """Write the running totals into the index row."""
        if not self.row_ready:
            return
        try:
            from storage.repository_devagent import (
                repo_devagent_save_thread_meta,
            )
            repo_devagent_save_thread_meta(self.thread_id, {
                "message_count": self.messages,
                "total_bytes": self.bytes_total,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "tokens_cache": self.tokens_cache,
                "last_message_at": _now(),
                "transcript_path": self.rel_path or None,
                "transcript_bytes": self.bytes_total,
                "content_schema": 1,
            })
        except Exception as exc:  # pragma: no cover - index is best effort
            log.warning("transcript index flush failed: %s", exc)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0