"""Breadth-limited directory tree walking for the list_files tool.

Pure helpers with no dependency on the executor: the caller supplies how a
path becomes project-relative, how protection is decided and which directory
names to skip. Kept apart from the tool so the traversal can be tested and
reasoned about on its own.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Set, Tuple


def _sort_key(path: Path):
    """Sort directories before files, then by lowercase name."""
    return (not path.is_dir(), path.name.lower())


def visible_children(folder: Path) -> List[Path]:
    """Return the sorted children of *folder*, or an empty list if unreadable."""
    try:
        return sorted(folder.iterdir(), key=_sort_key)
    except (OSError, PermissionError):
        return []


def make_file_entry(path: Path, to_relative: Callable[[Path], str],
                    is_protected: Callable[[Path], bool]) -> Dict[str, Any]:
    """Build one file entry: relative path, size in bytes and the protected flag."""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return {
        "path": safe_rel(path, to_relative),
        "size_bytes": size,
        "protected": is_protected(path),
    }


def safe_rel(path: Path, to_relative: Callable[[Path], str]) -> str:
    """Return the project-relative form of *path*, falling back to its string form."""
    try:
        return to_relative(path)
    except ValueError:
        return str(path)


def _list_dir_entry(child: Path, depth: int, depth_n: int,
                    dirs_out: List[Dict[str, Any]],
                    to_relative: Callable[[Path], str]) -> bool:
    """List one subdirectory and report whether the walk should descend into it.

    At the depth boundary the directory is listed with the files directly
    inside it but not descended into, so the walk stops there.
    """
    dir_entry: Dict[str, Any] = {"path": safe_rel(child, to_relative), "files": []}
    if depth + 1 < depth_n:
        dirs_out.append(dir_entry)
        return True
    try:
        for inner in sorted(child.iterdir(), key=_sort_key):
            if inner.is_file():
                dir_entry["files"].append(safe_rel(inner, to_relative))
    except (OSError, PermissionError):
        pass
    dirs_out.append(dir_entry)
    return False


def walk_tree(base: Path, depth_n: int, to_relative: Callable[[Path], str],
              is_protected: Callable[[Path], bool],
              skip_dirs: Set[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Walk *base* down to *depth_n* levels and split the result in two lists.

    Files directly inside a visited directory go to the flat ``files`` list.
    Every listed subdirectory carries the relative paths of the files DIRECTLY
    inside it in its ``files`` key. Directories deeper than *depth_n* are
    neither listed nor expanded; at the depth boundary the directory itself is
    listed with its direct files but not descended into.

    *depth_n* is expected to be already normalised to a positive integer by
    the caller. Each directory is visited at most once, so a symlink loop
    cannot hang the walk.

    Returns ``(files, dirs)``.
    """
    files_out: List[Dict[str, Any]] = []
    dirs_out: List[Dict[str, Any]] = []
    seen_dirs: Set[str] = set()

    def _walk(folder: Path, depth: int) -> None:
        direct_files: List[str] = []
        nested_dirs: List[Path] = []
        for child in visible_children(folder):
            if child.is_file():
                files_out.append(make_file_entry(child, to_relative, is_protected))
                direct_files.append(safe_rel(child, to_relative))
            elif child.is_dir() and child.name not in skip_dirs:
                child_rel = safe_rel(child, to_relative)
                if child_rel in seen_dirs:
                    continue
                seen_dirs.add(child_rel)
                if _list_dir_entry(child, depth, depth_n, dirs_out, to_relative):
                    nested_dirs.append(child)

        if depth > 0:
            # Attach the direct files to the dir entry the parent created.
            own_rel = safe_rel(folder, to_relative)
            for dir_entry in dirs_out:
                if dir_entry.get("path") == own_rel:
                    dir_entry["files"] = direct_files
                    break

        for child in nested_dirs:
            _walk(child, depth + 1)

    _walk(base, 0)
    return files_out, dirs_out


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
