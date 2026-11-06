"""Whole-file rewrites: staging a draft, applying or discarding it."""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.i18n import invalidate_langs_cache

from .deps import ToolMixin
from .text_utils import _validate_python_syntax


class EditingTools(ToolMixin):
    """Whole-file rewrites: staging a draft, applying or discarding it."""

    def propose_file(
        self,
        path: str,
        content: str,
        note: str = "",
        auto_apply: bool = True,
        allow_empty: bool = False,
    ) -> Dict[str, Any]:
        """Stage a full-content rewrite AND apply it immediately.

        This is the ONLY edit tool. The LLM always emits the COMPLETE new file
        text; there is no separate "new file" vs "rewrite" tool and no
        fragment/patch path -- propose_file handles both create and rewrite.

        Safety guards:
          - Python syntax is validated BEFORE any write (_.py_ files).
          - Empty content on an EXISTING file is REJECTED unless *allow_empty*
            is explicitly True.  This prevents accidental file truncation.

        If auto_apply=True (default): stages, applies, verifies, and returns
        the actual write result. The response includes 'verified=True',
        'backup_version', 'size_before', and 'size_after' on success, or
        'ok=False' with error details on failure.

        If auto_apply=False: only stages the draft (for manual approval mode).
        The caller must call apply_edit or discard_edit to complete the action.
        """
        # Validate Python syntax BEFORE any write (stdlib `ast` only).
        # Prevents broken .py files from ever being staged or applied.
        if path.endswith(".py"):
            syntax_err = _validate_python_syntax(content)
            if syntax_err:
                return {
                    "ok": False,
                    "path": path,
                    "error": syntax_err,
                    "errors": [syntax_err],
                    "syntax_error": True,
                    "verified": False,
                    "wrote_file": False,
                }

        try:
            src = self.deps().config.resolve_in_project(path)
        except ValueError as e:
            return {"ok": False, "path": path, "errors": [str(e)]}

        is_new = not src.exists()
        size_before: Optional[int] = None

        if not is_new:
            if not src.is_file():
                return {
                    "ok": False,
                    "path": self.deps().config.to_project_relative(path),
                    "errors": [f"'{path}' exists but is not a regular file."],
                }
            try:
                size_before = src.stat().st_size
            except OSError:
                size_before = 0

            # Guard against accidental file clearing.
            if not allow_empty and len(content.strip()) == 0:
                return {
                    "ok": False,
                    "path": self.deps().config.to_project_relative(path),
                    "error": (
                        "Refusing to wipe an existing file with empty content. "
                        "If you really intend to clear the file, set allow_empty=True."
                    ),
                    "errors": [
                        "Refusing to wipe an existing file with empty content. "
                        "Set allow_empty=True to confirm."
                    ],
                    "size_before": size_before,
                    "verified": False,
                    "wrote_file": False,
                }

        try:
            # Stage the draft
            res = self.writer.stage_draft_full(path, content)
        except Exception as e:
            return {
                "ok": False,
                "path": path,
                "error": f"Failed to stage draft: {e}",
                "errors": [f"Failed to stage draft: {e}"],
                "verified": False,
                "wrote_file": False,
            }
        if not res.ok:
            return {
                "ok": False,
                "path": res.rel_path,
                "error": (res.errors or ["Unknown staging error"])[0],
                "errors": res.errors,
                "verified": False,
                "wrote_file": False,
            }

        if not auto_apply:
            # Manual mode: return draft info, do NOT apply
            return {
                "ok": True,
                "path": res.rel_path,
                "draft_path": res.draft_path,
                "is_new": is_new,
                "new_text": content,
                "diff": res.diff,
                "note": note,
            }

        # Auto-apply: apply the draft and return the actual write result
        try:
            apply_res = self.writer.apply_draft(path, note=note)
        except Exception as e:
            return {
                "ok": False,
                "path": res.rel_path,
                "error": f"Failed to apply draft: {e}",
                "errors": [f"Failed to apply draft: {e}"],
                "verified": False,
                "backup_version": None,
                "diff": res.diff,
                "draft_path": res.draft_path,
                "wrote_file": False,
            }
        if not apply_res.ok:
            return {
                "ok": False,
                "path": res.rel_path,
                "error": apply_res.error,
                "errors": [apply_res.error],
                "verified": False,
                "backup_version": apply_res.backup_version,
                "diff": res.diff,
                "draft_path": res.draft_path,
                "wrote_file": False,
            }

        # If the written file is inside langs/, invalidate the language cache
        # so newly added/edited languages appear without restart.
        rel_path = res.rel_path
        if rel_path.startswith("langs/"):
            invalidate_langs_cache()

        # Determine the final size on disk.
        size_after: Optional[int] = None
        try:
            written = self.deps().config.resolve_in_project(path)
            if written.exists():
                size_after = written.stat().st_size
        except (OSError, ValueError):
            pass

        result: Dict[str, Any] = {
            "ok": True,
            "path": rel_path,
            "is_new": is_new,
            "new_text": content,
            "diff": res.diff,
            "note": note,
            "applied": True,
            "backup_version": apply_res.backup_version,
            "verified": True,
            "verified_text": apply_res.verified_text,
            "chars_expected": len(content),
            "chars_written": len(apply_res.verified_text or ""),
        }
        if size_before is not None:
            result["size_before"] = size_before
        if size_after is not None:
            result["size_after"] = size_after
        return result

    def apply_edit(self, path: str, note: str = "") -> Dict[str, Any]:
        """Apply a staged draft to source (backup -> write -> verify -> changelog).

        After writing, the file is read back and compared byte-for-byte with
        the staged draft. The response includes `verified=True` on success;
        if the read-back content differs (e.g. due to a permission or FS
        issue), `ok=False` and `verified=False` are returned.

        This tool is called by the agent loop for manual-mode approval, NOT
        by the LLM directly.
        """
        res = self.writer.apply_draft(path, note=note)
        if not res.ok:
            return {
                "ok": False,
                "path": res.rel_path,
                "error": res.error,
                "verified": False,
            }

        # If the written file is inside langs/, invalidate the language cache
        # so newly added/edited languages appear without restart.
        rel_path = res.rel_path
        if rel_path.startswith("langs/"):
            invalidate_langs_cache()

        return {
            "ok": True,
            "path": rel_path,
            "backup_version": res.backup_version,
            "message": res.message,
            "verified": True,
            "bytes_written": len((res.verified_text or "").encode(self.deps().config.DEFAULT_ENCODING)),
        }

    def discard_edit(self, path: str) -> Dict[str, Any]:
        """Discard a staged draft. Called by the agent loop, NOT by the LLM."""
        discarded = self.writer.discard_draft(path)
        return {"ok": True, "discarded": discarded,
                "path": self.deps().config.to_project_relative(path)}


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
