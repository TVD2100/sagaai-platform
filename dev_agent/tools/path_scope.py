"""Which folder a tool call addresses, judged by its path arguments.

A call is allowed before a project is chosen only when EVERY target it names
lies in a root that does not depend on a project: the employee's working
memory or the dialog's own files folder. One project-relative target in a
batch is enough to require a folder - a half-allowed batch would silently
read a file the user never opened.

The predicates live here rather than next to the guard because they are pure
path questions, and the guard is easier to read when it only has to combine
them.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

#: Argument names that may carry a target path, in the order they are read.
_TARGET_KEYS = ("path", "subdir", "file_path")


def all_targets(args: Optional[Dict[str, Any]], predicate: Callable[[str], bool]) -> bool:
    """True when every path argument of *args* satisfies *predicate*.

    Args:
        args: the tool arguments; anything but a mapping yields False.
        predicate: the test applied to each target string.

    Returns:
        True only when at least one target was found and all of them pass.
        An exception inside *predicate* counts as a refusal, never as a pass.
    """
    if not isinstance(args, dict):
        return False
    targets: List[str] = []
    for key in _TARGET_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            targets.append(value)
    files = args.get("files")
    if isinstance(files, str) and files.strip():
        targets.append(files)
    elif isinstance(files, (list, tuple)):
        targets.extend(f for f in files if isinstance(f, str) and f.strip())
    if not targets:
        return False
    try:
        return all(predicate(t) for t in targets)
    except Exception:
        return False


def addresses_memory(args: Optional[Dict[str, Any]], config: Any) -> bool:
    """True when every target points into the working memory of the caller."""
    return all_targets(args, config.is_memory_path)


def addresses_dialog_files(args: Optional[Dict[str, Any]], config: Any) -> bool:
    """True when every target points into the dialog's own files folder."""
    return all_targets(args, config.is_thread_files_path)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
