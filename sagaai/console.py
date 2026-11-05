"""
sagaai.console - how a command's result reaches the terminal.

One object per run, two modes, and the streams are not interchangeable:

- **stdout** carries the RESULT and nothing else. In ``--json`` mode it is
  exactly one JSON object; a script pipes it into ``jq`` and never has to
  strip a progress line.
- **stderr** carries everything else: progress, warnings, the uvicorn log, the
  "this may take a while" note. Redirecting it away must not lose the answer.

The human mode is not a prettified JSON dump. It is built from the report a
command already produces: sections with a title, ``label: value`` rows, a
status column and tables. That is what makes ``sagaai doctor`` readable as a
message in a chat window - the common way a user asks for help.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: Marker of every status in the human mode. Text, not an emoji: a terminal
#: with an exotic locale still renders these, and a log file stays greppable.
STATUS_MARK = {"ok": "[ ok ]", "warn": "[warn]", "error": "[fail]",
               "skipped": "[ -- ]"}


class Console:
    """Formats one command's result for a human or for a machine."""

    def __init__(self, json_mode: bool = False,
                 stream: Any = None, err_stream: Any = None) -> None:
        self.json_mode = bool(json_mode)
        self.stream = stream if stream is not None else sys.stdout
        self.err_stream = err_stream if err_stream is not None else sys.stderr
        self._lines: List[str] = []

    # ── Progress and warnings (stderr) ─────────────────────────────────────

    def note(self, message: str) -> None:
        """Write a progress or context line to stderr.

        Suppressed in JSON mode: a machine reader must be able to consume the
        stream without filtering, and progress is not part of the answer.
        """
        if self.json_mode:
            return
        print(str(message), file=self.err_stream, flush=True)

    def warn(self, message: str) -> None:
        """Write a warning to stderr, in both modes."""
        print("warning: " + str(message), file=self.err_stream, flush=True)

    # ── Human-readable building blocks ─────────────────────────────────────

    def title(self, text: str) -> None:
        """Start a section."""
        if self.json_mode:
            return
        self._lines.append("")
        self._lines.append(str(text))
        self._lines.append("-" * max(3, min(72, len(str(text)))))

    def line(self, text: str = "") -> None:
        """Append one plain line."""
        if self.json_mode:
            return
        self._lines.append(str(text))

    def field(self, label: str, value: Any) -> None:
        """Append a ``label: value`` row."""
        if self.json_mode:
            return
        self._lines.append("  %s: %s" % (str(label), _as_text(value)))

    def status(self, name: str, status: str, summary: str = "") -> None:
        """Append a check row: fixed-width marker, name, summary."""
        if self.json_mode:
            return
        mark = STATUS_MARK.get(str(status), "[????]")
        self._lines.append("  %s %-20s %s" % (mark, str(name), str(summary)))

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
        """Append a left-aligned table; a wide last column is not truncated."""
        if self.json_mode:
            return
        head = [str(item) for item in headers]
        body = [[_as_text(cell) for cell in row] for row in rows]
        widths = [len(item) for item in head]
        for row in body:
            for index, cell in enumerate(row[:len(widths)]):
                widths[index] = max(widths[index], len(cell))
        self._lines.append("  " + "  ".join(
            head[i].ljust(widths[i]) for i in range(len(head))).rstrip())
        self._lines.append("  " + "  ".join("-" * widths[i]
                                            for i in range(len(head))))
        for row in body:
            cells = [row[i] if i < len(row) else "" for i in range(len(head))]
            self._lines.append("  " + "  ".join(
                cells[i].ljust(widths[i]) for i in range(len(head))).rstrip())

    def bullet(self, text: str) -> None:
        """Append a bullet item."""
        if self.json_mode:
            return
        self._lines.append("  - " + str(text))

    def blank(self) -> None:
        """Append an empty line."""
        if self.json_mode:
            return
        self._lines.append("")

    # ── Result ─────────────────────────────────────────────────────────────

    def emit(self, command: str, data: Optional[Dict[str, Any]] = None,
             exit_code: int = 0, version: str = "") -> None:
        """Write the successful result of one command.

        In JSON mode this is the whole stdout: one object, no trailing prose.
        In human mode it flushes the accumulated sections and a final status.
        """
        if self.json_mode:
            payload: Dict[str, Any] = {
                "ok": True,
                "command": str(command),
                "exit_code": int(exit_code),
                "version": str(version),
                "data": dict(data or {}),
            }
            self._write_json(payload)
            return
        for line in self._lines:
            print(line, file=self.stream)
        self._lines = []
        self.stream.flush()

    def emit_error(self, command: str, error: Any,
                   exit_code: int = 1, version: str = "") -> None:
        """Write the failure of one command.

        ``error`` is a :class:`sagaai.errors.CliError`. The human mode prints a
        short block: what failed, the machine code, the hint. A traceback is
        never printed - the detail belongs in the log of the failing layer.
        """
        payload_error = error.to_dict() if hasattr(error, "to_dict") else {
            "code": "failure", "message": str(error)}
        if self.json_mode:
            self._write_json({
                "ok": False,
                "command": str(command),
                "exit_code": int(exit_code),
                "version": str(version),
                "error": payload_error,
            })
            return
        for line in self._lines:
            print(line, file=self.stream)
        self._lines = []
        print("", file=self.stream)
        print("[fail] %s failed: %s" % (str(command),
                                        payload_error.get("message", "")),
              file=self.stream)
        print("  code: %s (exit %d)" % (payload_error.get("code", ""),
                                        int(exit_code)), file=self.stream)
        hint = str(payload_error.get("hint") or "")
        if hint:
            print("  hint: " + hint, file=self.stream)
        self.stream.flush()

    def _write_json(self, payload: Dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        print(text, file=self.stream)
        self.stream.flush()


def _as_text(value: Any) -> str:
    """Render one cell for the human mode."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_as_text(item) for item in value) or "-"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def human_bytes(size: Any) -> str:
    """Render a byte count the way a person reads it."""
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%.1f GB" % value


def human_seconds(value: Any) -> str:
    """Render a duration the way a person reads it."""
    try:
        total = int(float(value or 0))
    except (TypeError, ValueError):
        return "0s"
    if total < 60:
        return "%ds" % total
    if total < 3600:
        return "%dm %ds" % (total // 60, total % 60)
    return "%dh %dm" % (total // 3600, (total % 3600) // 60)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
