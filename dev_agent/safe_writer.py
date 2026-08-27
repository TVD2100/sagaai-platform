# Protected, reversible writes for DevAgent.
#
# Pipeline (mirrors SagaAI_Architecture_v3-3.md section 7.2 "Work Cycle"):
#   1. DevAgent produces the COMPLETE new file text -> stage_draft_full()
#      writes it to the workspace draft area (NEVER directly to source).
#   2. UI shows render_diff() -> the diff between source and draft.
#   3. On approval -> apply_draft(): create backup -> write source ->
#      append CHANGELOG.
#
# There is no fragment/patch path anymore: every edit is a full-file rewrite
# staged as a single draft, which keeps the write path simple and auditable.
#
# Two-level protection of the Inviolable Core (section 7.1):
#   - PROTECTED_FILES rule lives in DevAgent's system prompt.
#   - check_protected() here HARD-BLOCKS any write to a protected file, even if
#     the LLM (or a bug) tries to bypass the prompt rule.

import difflib
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import config
from .backup_manager import BackupManager


class ProtectedFileError(Exception):
    """Raised when a write targets an Inviolable-Core file."""


@dataclass
class DraftResult:
    ok: bool
    rel_path: str = ""
    draft_path: str = ""
    new_text: Optional[str] = None
    diff: str = ""
    errors: Optional[List[str]] = None


@dataclass
class ApplyResult:
    ok: bool
    rel_path: str = ""
    backup_version: Optional[int] = None
    message: str = ""
    error: str = ""
    verified_text: Optional[str] = None   # actual content read back after write


class SafeWriter:
    def __init__(self, backup_manager: Optional[BackupManager] = None):
        config.ensure_runtime_dirs()
        self.backups = backup_manager or BackupManager()

    # ─── protection ─────────────────────────────────────────────────────────--
    def check_protected(self, path) -> None:
        """Raise ProtectedFileError if `path` is part of the Inviolable Core."""
        if config.is_protected(path):
            rel = config.to_project_relative(path) if _safe_rel(path) else str(path)
            raise ProtectedFileError(
                f"'{rel}' is a PROTECTED file (Inviolable Core) and cannot be "
                f"modified by DevAgent."
            )

    # ─── drafting ───────────────────────────────────────────────────────────--
    def _draft_path_for(self, rel: str) -> Path:
        # Mirror the project tree inside workspace/ so drafts are unambiguous.
        return config.WORKSPACE_DIR / rel

    def stage_draft_full(self, path, new_text: str) -> DraftResult:
        """Stage a full-content draft for `path`.

        This is the single drafting mechanism: the caller supplies the complete
        new file text, which is written to the workspace draft area (never
        directly to source) and diffed against the current content for review.
        """
        try:
            self.check_protected(path)
        except ProtectedFileError as e:
            return DraftResult(ok=False, errors=[str(e)])

        rel = config.to_project_relative(path)
        src = config.resolve_in_project(path)
        try:
            original = src.read_text(encoding=config.DEFAULT_ENCODING) if src.exists() else ""
        except UnicodeDecodeError as e:
            return DraftResult(
                ok=False,
                rel_path=rel,
                errors=[
                    f"File is not valid UTF-8 text (decode error at byte {e.start}). "
                    "propose_file operates on UTF-8 text files; re-encode "
                    "the file to UTF-8 first."
                ],
            )
        draft_file = self._draft_path_for(rel)
        draft_file.parent.mkdir(parents=True, exist_ok=True)
        draft_file.write_text(new_text, encoding=config.DEFAULT_ENCODING)
        return DraftResult(
            ok=True,
            rel_path=rel,
            draft_path=str(draft_file),
            new_text=new_text,
            diff=render_diff(original, new_text, rel),
        )

    # ─── applying ───────────────────────────────────────────────────────────--
    def apply_draft(self, path, note: str = "") -> ApplyResult:
        """Apply a previously staged draft: backup current -> write -> verify -> log.

        If post-write verification fails (written content differs from the draft),
        the error message includes the lengths of the expected and actual content
        and the draft path so the caller can diagnose the issue.
        """
        try:
            self.check_protected(path)
        except ProtectedFileError as e:
            return ApplyResult(ok=False, error=str(e))

        rel = config.to_project_relative(path)
        draft_file = self._draft_path_for(rel)
        if not draft_file.exists():
            return ApplyResult(ok=False, rel_path=rel,
                               error=f"No staged draft for {rel}. Stage one first.")

        new_text = draft_file.read_text(encoding=config.DEFAULT_ENCODING)
        dst = config.resolve_in_project(path)

        backup_version = None
        if dst.exists():
            try:
                entry = self.backups.create_backup(path, note=note or "pre-edit backup")
                backup_version = entry.version
            except Exception as e:
                return ApplyResult(ok=False, rel_path=rel,
                                   error=f"Backup failed, aborting write: {e}")

        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(new_text, encoding=config.DEFAULT_ENCODING)

        # ── Force flush to disk (critical for cloud-synced directories) ─────
        try:
            import fcntl
            fd = os.open(str(dst), os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
        except Exception:
            pass

        # ── Post-write verification with retry for cloud-sync delay ──────────
        max_retries = 3
        for attempt in range(max_retries):
            try:
                written_back = dst.read_text(encoding=config.DEFAULT_ENCODING)
            except Exception as e:
                return ApplyResult(ok=False, rel_path=rel,
                                   backup_version=backup_version,
                                   error=f"Verification read failed: {e}")
            if written_back == new_text:
                break
            if attempt < max_retries - 1:
                time.sleep(0.3)

        if written_back != new_text:
            # ── Enhanced error: include length info to help diagnose I/O quirks ─
            return ApplyResult(ok=False, rel_path=rel,
                               backup_version=backup_version,
                               error=(
                                   "Post-write verification failed: "
                                   f"written content ({len(written_back)} chars) "
                                   f"differs from draft ({len(new_text)} chars). "
                                   f"Draft was at {draft_file}. "
                                   "The file may be locked or the filesystem may "
                                   "have silently truncated the write."
                               ),
                               verified_text=written_back)

        # Clear the consumed draft.
        try:
            draft_file.unlink()
        except Exception:
            pass

        # Changelog is best-effort; a failure here must not undo the write.
        self._append_changelog(rel, note, backup_version)
        msg = f"Applied changes to {rel}."
        if backup_version is not None:
            msg += f" Backup v{backup_version} saved."
        return ApplyResult(ok=True, rel_path=rel,
                           backup_version=backup_version, message=msg,
                           verified_text=written_back)

    def discard_draft(self, path) -> bool:
        """Delete a staged draft without applying it."""
        rel = config.to_project_relative(path)
        draft_file = self._draft_path_for(rel)
        if draft_file.exists():
            draft_file.unlink()
            return True
        return False

    # ─── changelog ──────────────────────────────────────────────────────────--
    def _append_changelog(self, rel: str, note: str, version: Optional[int]) -> None:
        """Append a line to CHANGELOG.md. Best-effort: errors are logged to
        stderr but never propagated - a changelog write failure must not undo
        the actual file change."""
        ts = datetime.now().isoformat(timespec="seconds")
        line = f"- [{ts}] {rel}"
        if version is not None:
            line += f" (backup v{version})"
        if note:
            line += f" - {note}"
        line += "\n"
        cf = config.CHANGELOG_FILE
        try:
            # Adaptive changelog header: only mention SagaAI if working on the install.
            if config.WORKING_ON_INSTALL:
                default_header = "# SagaAI Changelog\n\nDevAgent change log.\n\n"
            else:
                workspace_name = config.PROJECT_ROOT.name or "project"
                default_header = f"# {workspace_name} Changelog\n\nAutomatically maintained by DevAgent.\n\n"
            header = "" if cf.exists() else default_header
            with cf.open("a", encoding=config.DEFAULT_ENCODING) as f:
                if header:
                    f.write(header)
                f.write(line)
        except OSError as e:
            # Best-effort: log the error but never crash the apply.
            import sys
            print(f"[DevAgent] changelog write failed: {e}", file=sys.stderr)


def _safe_rel(path) -> bool:
    try:
        config.to_project_relative(path)
        return True
    except ValueError:
        return False


def render_diff(original: str, updated: str, filename: str = "file") -> str:
    """Unified diff between two texts (for UI display & review)."""
    diff = difflib.unified_diff(
        original.splitlines(keepends=False),
        updated.splitlines(keepends=False),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        lineterm="",
    )
    return "\n".join(diff)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
