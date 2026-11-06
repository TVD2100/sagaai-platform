"""Reading, listing and verifying project files, plus per-file backups."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .deps import ToolMixin
from .file_tree import walk_tree

# Directory names never listed or descended into by list_files.
_SKIP_DIRS = frozenset({
    "__pycache__", ".git", ".dev_agent", "backups", "workspace", "history",
    ".pytest_cache", "node_modules", ".venv", "venv",
})


class FilesTools(ToolMixin):
    """Reading, listing and verifying project files, plus per-file backups."""

    def read_file(
        self,
        path: str,
        with_line_numbers: bool = True,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Read a project file, always in its entirety.

        When *offset* (1-based) or *limit* is provided, only the requested
        window is returned.  Line numbers always refer to absolute positions
        in the original file, not the window.

        The file size is checked against the configured maximum before reading.
        """
        # Validate offset/limit arguments.
        if offset is not None and (not isinstance(offset, int) or offset < 1):
            return {"ok": False, "error": "offset must be an integer >= 1 (1-based)."}
        if limit is not None and (not isinstance(limit, int) or limit < 1):
            return {"ok": False, "error": "limit must be an integer >= 1."}

        try:
            src = self.deps().config.resolve_in_project(path)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not src.exists():
            return {"ok": False, "error": f"File not found: {path}"}
        if src.stat().st_size > self.deps().config.MAX_FILE_SIZE_BYTES:
            return {"ok": False, "error": f"File too large (> {self.deps().config.MAX_FILE_SIZE_BYTES} bytes)"}

        text = src.read_text(encoding=self.deps().config.DEFAULT_ENCODING)
        lines = text.split("\n")
        total = len(lines)

        # Apply offset/limit window if requested.
        if offset is not None or limit is not None:
            start_idx = (offset or 1) - 1         # convert to 0-based
            start_idx = max(0, min(start_idx, total))
            end_idx = total if limit is None else min(total, start_idx + limit)
            lines = lines[start_idx:end_idx]

        if with_line_numbers:
            width = len(str(total))
            # line numbers refer to the original absolute positions
            base = 1 if (offset is None) else (offset or 1)
            content = "\n".join(
                f"{i:>{width}}|{ln}"
                for i, ln in enumerate(lines, start=base)
            )
        else:
            content = "\n".join(lines)

        result: Dict[str, Any] = {
            "ok": True,
            "path": self.deps().config.to_project_relative(path),
            "total_lines": total,
            "protected": self.deps().config.is_protected(path),
            "content": content,
        }
        if offset is not None:
            result["offset"] = offset
        if limit is not None:
            result["limit"] = limit
        if offset is not None or limit is not None:
            result["window_lines"] = len(lines)
            last_shown = (offset or 1) - 1 + len(lines)
            result["remaining"] = max(0, total - last_shown)
            if total <= 2000:
                result["hint"] = (
                    f"File is only {total} lines; consider reading it "
                    f"whole without offset/limit."
                )
        return result

    def list_files(self, subdir: str = "", max_depth: int = 1) -> Dict[str, Any]:
        """List files and directories under the workspace root or *subdir*.

        Depth is configurable: ``max_depth=1`` (default) lists only the first
        level (no recursion); deeper values list several levels at once. Use
        this instead of several sequential list_files calls when you need a
        wider view (e.g. a project tree) - one call with ``max_depth=2..3``
        replaces many round trips.

        Returns ALL files (any extension) and ALL directories except
        noise folders like __pycache__, .git, .dev_agent, etc. Each dir
        entry carries the files DIRECTLY inside it.

        Args:
            subdir: subdirectory relative to project root (default: root).
            max_depth: how many directory levels below base to expand
                (default 1 = no recursion). Values <= 0 behave as 1.

        Returns:
            {"ok": True, "base": "...", "max_depth": N, "count": N,
             "files": [...], "dirs": [...]}
            Each file entry: {"path": "...", "size_bytes": N, "protected": bool}
            Each dir entry:  {"path": "...", "files": [...]}
        """
        try:
            base = self.deps().config.resolve_in_project(subdir) if subdir else self.deps().config.PROJECT_ROOT.resolve()
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not base.exists() or not base.is_dir():
            return {"ok": False, "error": f"Directory not found: {subdir}"}

        try:
            depth_n = max(1, int(max_depth))
        except (TypeError, ValueError):
            depth_n = 1

        files_out, dirs_out = walk_tree(
            base, depth_n,
            to_relative=self.deps().config.to_project_relative,
            is_protected=self.deps().config.is_protected,
            skip_dirs=_SKIP_DIRS,
        )

        return {
            "ok": True,
            "base": subdir or ".",
            "max_depth": depth_n,
            "count": len(files_out),
            "files": files_out,
            "dirs": dirs_out,
        }

    def verify_file(
        self,
        path: str,
        expected_substrings: Optional[List[str]] = None,
        unexpected_substrings: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Read a file back and check it contains/lacks given substrings.

        Intended for use immediately after `apply_edit`: the agent can pass
        a few short, unique fragments it expects to find (or NOT find) in the
        new content. Returns a structured report so the agent can decide
        whether to retry, roll back, or proceed.
        """
        try:
            src = self.deps().config.resolve_in_project(path)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not src.exists():
            return {"ok": False, "error": f"File not found: {path}"}
        if not src.is_file():
            return {"ok": False,
                    "error": f"Not a regular file: {path}"}
        try:
            text = src.read_text(encoding=self.deps().config.DEFAULT_ENCODING)
        except UnicodeDecodeError as e:
            return {
                "ok": False,
                "path": self.deps().config.to_project_relative(path),
                "error": (
                    f"File is not valid UTF-8 text (decode error at byte "
                    f"{e.start})."
                ),
            }
        except OSError as e:
            return {"ok": False,
                    "path": self.deps().config.to_project_relative(path),
                    "error": f"Read failed: {e}"}

        expected_substrings = list(expected_substrings or [])
        unexpected_substrings = list(unexpected_substrings or [])
        missing = [s for s in expected_substrings if s not in text]
        present_unexpected = [s for s in unexpected_substrings if s in text]
        ok = not missing and not present_unexpected
        return {
            "ok": ok,
            "path": self.deps().config.to_project_relative(path),
            "total_lines": len(text.split("\n")),
            "bytes": len(text.encode(self.deps().config.DEFAULT_ENCODING)),
            "missing_expected": missing,
            "present_unexpected": present_unexpected,
        }

    def create_backup(self, path: str, note: str = "") -> Dict[str, Any]:
        try:
            entry = self.backups.create_backup(path, note=note)
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "version": entry.version, "timestamp": entry.timestamp,
                "checksum": entry.checksum[:12]}

    def restore_backup(self, path: str, version: Optional[int] = None) -> Dict[str, Any]:
        try:
            entry = self.backups.restore_backup(path, version=version)
        except KeyError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "restored_version": entry.version,
                "path": self.deps().config.to_project_relative(path)}

    def show_history(self, path: str) -> Dict[str, Any]:
        return {"ok": True, **self.backups.history_summary(path)}


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
