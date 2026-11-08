# -*- coding: utf-8 -*-
"""
core.marketplace.package_pin - the update baseline of an installation.

An update decides whether the user changed a value by comparing the installed
config against the manifest of the installed version, and that manifest is
read from the cached package. The cache has a lifetime, so without a pin an
installation older than that lifetime answers "the user changed everything"
and an update silently moves nothing. The package of the version actually
installed is therefore pinned for as long as it is installed; the package of
a version the agent moved away from is released once no installation refers
to it any more.

The cache index belongs to :mod:`core.marketplace.store` and the rows to
:mod:`core.marketplace.registry`; both are imported lazily here, so this
module never closes an import cycle and a test patch installed on either
module stays visible.
"""
from typing import List


def previous_package_sha(entity_id: str) -> str:
    """Return the package sha *entity_id* had before this run.

    Read BEFORE the registry row is rewritten: afterwards the row names the
    new package and the previous one can no longer be discovered. An empty
    answer means a first install or an unreadable registry.
    """
    from core.marketplace import registry

    try:
        row = registry.get_installed("agent", entity_id) or {}
    except Exception:
        return ""
    return str(row.get("package_sha256") or "")


def refresh_package_pin(package_sha: str, previous_sha: str = "") -> None:
    """Pin the installed package and release the one it replaced.

    Pinning is best effort: an installation that already committed must not
    fail because the cache index could not be written. Releasing is equally
    forgiving, and it happens only when no installation still names the old
    package as its baseline.

    Parameters:
        package_sha: sha256 of the package now installed.
        previous_sha: sha256 the same installation had before this run, or an
            empty string for a first install.
    """
    from core.marketplace import registry, store

    if package_sha:
        try:
            store.pin_package(package_sha)
        except Exception:
            pass
    if not previous_sha or previous_sha == package_sha:
        return
    try:
        still_used: List[dict] = [
            row for row in registry.list_installed("agent")
            if str(row.get("package_sha256") or "") == previous_sha
        ]
    except Exception:
        return
    if still_used:
        return
    try:
        store.pin_package(previous_sha, False)
    except Exception:
        pass


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
