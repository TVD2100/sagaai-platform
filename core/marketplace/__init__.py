"""
core.marketplace - entity catalog, installer and update engine.

The package installs agents, skills, knowledge bases and MCP connectors
from a catalog repository into the running installation, keeping user data
intact and every run reversible.

Modules:
  * ``config``    - settings (`marketplace.*` keys of the platform config)
  * ``semver``    - strict SemVer parsing and range matching
  * ``store``     - filesystem layout, install mutex, atomic writes, caches
  * ``registry``  - SQLite registry of installed entities and removals
  * ``source``    - catalog transport with a conditional GET
  * ``verify``    - package hashes, archive limits, signature stub
  * ``rollback``  - restore a run from its backup
  * ``bootstrap`` - finish or undo runs interrupted by a crash
  * ``installer`` - skill installation (P1)
  * ``conflicts`` - local modifications and the three resolution paths (P2)
  * ``updates``   - update discovery and application (P3)

The public entry points are re-exported here so callers (UI, tools) do not
need to know the module layout.
"""
from __future__ import annotations

from core.marketplace.config import (  # noqa: F401
    allow_code,
    is_enabled,
    marketplace_settings,
    save_settings,
)
from core.marketplace.registry import (  # noqa: F401
    get_installed,
    is_removed,
    list_installed,
    list_removed,
    mark_removed,
    remove_installed,
    seed_local_orphans,
    tree_sha256,
    upsert_installed,
)
from core.marketplace.semver import (  # noqa: F401
    SemverError,
    compare,
    intersect,
    parse,
    satisfies,
)
from core.marketplace.store import (  # noqa: F401
    MarketplaceBusyError,
    MarketplaceError,
    install_lock,
)


__all__ = [
    "MarketplaceBusyError",
    "MarketplaceError",
    "SemverError",
    "allow_code",
    "compare",
    "get_installed",
    "install_lock",
    "intersect",
    "is_enabled",
    "is_removed",
    "list_installed",
    "list_removed",
    "mark_removed",
    "marketplace_settings",
    "parse",
    "remove_installed",
    "satisfies",
    "save_settings",
    "seed_local_orphans",
    "tree_sha256",
    "upsert_installed",
]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
