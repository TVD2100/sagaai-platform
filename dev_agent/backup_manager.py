# Versioned backups with a manifest for DevAgent.
#
# Before any write to a project file, a backup is created. Backups are stored
# under dev_agent/backups/<safe_relpath>/v<N>__<timestamp>.bak with a per-file
# manifest.json tracking the version chain.
#
# Architecture reference: SagaAI_Architecture_v3-3.md section 7.1
# ("Inviolable Core" recovery logic) and the DevAgent tool set (create_backup,
# restore_backup, show_history) in section 7.3.

import json
import hashlib
import shutil
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from . import config


def _safe_relpath_key(rel: str) -> str:
    """Turn a project-relative path into a single safe directory name.

    e.g. 'core/skills.py' -> 'core__skills.py'. Avoids nested-dir surprises and
    keeps one manifest per source file.
    """
    return rel.replace("/", "__")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode(config.DEFAULT_ENCODING)).hexdigest()


@dataclass
class BackupEntry:
    version: int
    timestamp: str          # ISO 8601
    backup_path: str        # relative to BACKUPS_DIR
    checksum: str           # sha256 of the backed-up content
    note: str = ""
    size_bytes: int = 0


class BackupManager:
    """Manages versioned backups for a single project rooted at PROJECT_ROOT."""

    def __init__(self, backups_dir: Optional[Path] = None):
        self.backups_dir = Path(backups_dir) if backups_dir else config.BACKUPS_DIR
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    # ─── internal manifest helpers ─────────────────────────────────────────────
    def _file_backup_dir(self, rel: str) -> Path:
        d = self.backups_dir / _safe_relpath_key(rel)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _manifest_path(self, rel: str) -> Path:
        return self._file_backup_dir(rel) / "manifest.json"

    def _load_manifest(self, rel: str) -> List[BackupEntry]:
        mp = self._manifest_path(rel)
        if not mp.exists():
            return []
        try:
            raw = json.loads(mp.read_text(encoding=config.DEFAULT_ENCODING))
            return [BackupEntry(**e) for e in raw]
        except Exception:
            return []

    def _save_manifest(self, rel: str, entries: List[BackupEntry]) -> None:
        mp = self._manifest_path(rel)
        mp.write_text(
            json.dumps([asdict(e) for e in entries], ensure_ascii=False, indent=2),
            encoding=config.DEFAULT_ENCODING,
        )

    # ─── public API ─────────────────────────────────────────────────────────--
    def create_backup(self, path, note: str = "") -> BackupEntry:
        """Snapshot the current on-disk content of `path`.

        Returns the created BackupEntry. Raises FileNotFoundError if the source
        does not exist (nothing to back up).
        """
        rel = config.to_project_relative(path)
        src = config.resolve_in_project(path)
        if not src.exists():
            raise FileNotFoundError(f"Cannot back up missing file: {rel}")

        content = src.read_text(encoding=config.DEFAULT_ENCODING)
        entries = self._load_manifest(rel)
        next_version = (entries[-1].version + 1) if entries else 1
        ts = datetime.now().isoformat(timespec="seconds")
        ts_safe = ts.replace(":", "-")
        backup_name = f"v{next_version}__{ts_safe}.bak"
        backup_file = self._file_backup_dir(rel) / backup_name
        backup_file.write_text(content, encoding=config.DEFAULT_ENCODING)

        entry = BackupEntry(
            version=next_version,
            timestamp=ts,
            backup_path=str(backup_file.relative_to(self.backups_dir).as_posix()),
            checksum=_sha256(content),
            note=note,
            size_bytes=len(content.encode(config.DEFAULT_ENCODING)),
        )
        entries.append(entry)
        self._rotate(rel, entries)
        self._save_manifest(rel, entries)
        return entry

    def _rotate(self, rel: str, entries: List[BackupEntry]) -> None:
        """Drop oldest backups beyond MAX_BACKUPS_PER_FILE (mutates entries)."""
        excess = len(entries) - config.MAX_BACKUPS_PER_FILE
        if excess <= 0:
            return
        for old in entries[:excess]:
            f = self.backups_dir / old.backup_path
            if f.exists():
                f.unlink()
        del entries[:excess]

    def list_versions(self, path) -> List[BackupEntry]:
        """Return the version history for a file, oldest → newest."""
        rel = config.to_project_relative(path)
        return self._load_manifest(rel)

    def get_backup_content(self, path, version: int) -> str:
        """Return the stored content of a specific backup version."""
        rel = config.to_project_relative(path)
        for e in self._load_manifest(rel):
            if e.version == version:
                bf = self.backups_dir / e.backup_path
                return bf.read_text(encoding=config.DEFAULT_ENCODING)
        raise KeyError(f"No backup v{version} for {rel}")

    def restore_backup(self, path, version: Optional[int] = None) -> BackupEntry:
        """Restore a file from a backup version (default: latest).

        IMPORTANT: this writes directly to the source file, bypassing the
        protection check, because restoring is a recovery operation. It still
        creates a fresh backup of the current (broken) state first, so the
        restore itself is reversible.
        """
        rel = config.to_project_relative(path)
        entries = self._load_manifest(rel)
        if not entries:
            raise KeyError(f"No backups available for {rel}")
        target = entries[-1] if version is None else next(
            (e for e in entries if e.version == version), None
        )
        if target is None:
            raise KeyError(f"No backup v{version} for {rel}")

        dst = config.resolve_in_project(path)
        # Snapshot current state before overwriting (recovery-safe).
        if dst.exists():
            try:
                self.create_backup(path, note=f"pre-restore snapshot (to v{target.version})")
            except Exception:
                pass

        content = self.get_backup_content(path, target.version)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding=config.DEFAULT_ENCODING)
        return target

    def history_summary(self, path) -> Dict[str, Any]:
        """Compact dict summary of a file's backup history (for tools/UI)."""
        rel = config.to_project_relative(path)
        entries = self._load_manifest(rel)
        return {
            "file": rel,
            "total_versions": len(entries),
            "versions": [
                {
                    "version": e.version,
                    "timestamp": e.timestamp,
                    "note": e.note,
                    "size_bytes": e.size_bytes,
                    "checksum": e.checksum[:12],
                }
                for e in entries
            ],
        }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
