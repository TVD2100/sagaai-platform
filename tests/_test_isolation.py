# -*- coding: utf-8 -*-
"""tests._test_isolation - helpers to isolate project modules between tests.

Kept as a plain importable module (not a fixture-only block inside
conftest.py) so the isolation machinery itself can be unit-tested.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Dict, List


# Prefixes of project packages that tests reload/restore wholesale.
APP_MODULE_PREFIXES = ("core", "storage", "ui")


def is_app_module(name: str) -> bool:
    """Return True when *name* belongs to a reloadable project package."""
    return name.startswith(APP_MODULE_PREFIXES)


def snapshot_app_modules() -> Dict[str, object]:
    """Return the current {name: module} mapping for app packages."""
    return {
        name: mod
        for name, mod in list(sys.modules.items())
        if is_app_module(name)
    }


def drop_app_modules() -> None:
    """Remove all app-package modules from sys.modules."""
    for name in list(sys.modules):
        if is_app_module(name):
            sys.modules.pop(name, None)


def names_created_since(snapshot: Dict[str, object]) -> List[str]:
    """Return app-module names currently present but missing from *snapshot*."""
    return [
        name for name in list(sys.modules)
        if is_app_module(name) and name not in snapshot
    ]


def restore_app_modules(snapshot: Dict[str, object]) -> None:
    """Put back the snapshot and drop modules created after it was taken."""
    for name, mod in snapshot.items():
        sys.modules[name] = mod
    for name in names_created_since(snapshot):
        sys.modules.pop(name, None)


@contextmanager
def isolated_app_modules():
    """Context manager: isolate app modules, then restore them on exit."""
    snapshot = snapshot_app_modules()
    drop_app_modules()
    try:
        yield snapshot
    finally:
        restore_app_modules(snapshot)
