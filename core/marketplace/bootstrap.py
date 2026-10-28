"""
core.marketplace.bootstrap - start-up recovery and one-time seeding.

A marketplace run records itself in ``history.json`` (``status:
"applying"``) and in ``state.json`` before it touches anything. If the
process dies in the middle - power loss, force-quit, a killed container -
the installation is left half-changed. ``recover()`` runs once on start-up
and undoes every run that never reached a terminal status, so the platform
always opens on a consistent state.

``bootstrap()` is the single entry point the application calls: it makes
sure the tables exist, finishes interrupted runs and seeds registry rows
for skills that predate the marketplace. It never performs a network
request - the catalog is only contacted when the user opens the section.
"""
from __future__ import annotations

import logging
from typing import Dict, List

from core.marketplace import config as marketplace_config
from core.marketplace import migration, registry, rollback, store


logger = logging.getLogger(__name__)


def recover() -> List[dict]:
    """Roll back every run left in the ``applying`` state.

    Returns one report per recovered run. A run whose backup metadata is
    missing cannot be undone safely, so it is marked ``failed`` with the
    reason recorded - silently forgetting it would hide a half-installed
    entity from the user.
    """
    reports: List[dict] = []
    for run in store.applying_runs():
        install_id = str(run.get("install_id") or "")
        if not install_id:
            continue
        try:
            reports.append(rollback.rollback_run(install_id, reason="recovery"))
        except Exception as exc:
            logger.warning("marketplace recovery failed for %s: %s", install_id, exc)
            store.update_run(
                install_id,
                status="failed",
                failed_at=store.isoformat_utc(),
                failure_reason=f"recovery failed: {exc}",
            )
            reports.append({"ok": False, "install_id": install_id, "error": str(exc)})
    state = store.get_state()
    if state is not None:
        store.clear_state()
    return reports


def bootstrap() -> Dict[str, object]:
    """Prepare the marketplace for use and return a summary.

    Steps: create the tables (idempotent), roll back interrupted runs and
    seed registry rows for skills installed before the marketplace existed.
    A disabled marketplace is a no-op apart from the table check: with
    ``marketplace.enabled = false`` the feature must leave no trace beyond
    its empty tables.
    """
    summary: Dict[str, object] = {
        "ok": True,
        "enabled": False,
        "recovered": [],
        "seeded": 0,
        "migrated": {"seeded": [], "carried": []},
    }
    try:
        registry.ensure_tables()
    except Exception as exc:
        logger.warning("marketplace tables unavailable: %s", exc)
        summary["ok"] = False
        summary["error"] = str(exc)
        return summary

    summary["enabled"] = marketplace_config.is_enabled()
    if not summary["enabled"]:
        return summary

    try:
        store.ensure_layout()
        summary["recovered"] = recover()
        summary["migrated"] = migration.migrate()
        summary["seeded"] = len(registry.seed_local_orphans())
        store.prune_cache()
    except Exception as exc:
        logger.warning("marketplace bootstrap step failed: %s", exc)
        summary["ok"] = False
        summary["error"] = str(exc)
    return summary
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
