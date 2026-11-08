# -*- coding: utf-8 -*-
"""
core.marketplace.bundle_catalog - catalog-level bundles: listing and planning.

A catalog-level bundle pins exact versions of several entities so one action
reproduces a whole set (``bundles/<id>.json``). Reading one is separated
from installing one: the list and the plan never touch the disk, and the
install path lives in ``core.marketplace.bundles``.

A bundle name may be an i18n mapping, so every name here is resolved for the
language the page is shown in, exactly like an entity name.
"""
from typing import List, Optional

from core.marketplace import source as source_mod


def _localized_name(value, lang: str) -> str:
    """Return *value* resolved for *lang*, or an empty string."""
    from core.marketplace.catalog import localized

    return localized(value, lang)


def catalog_bundles(
    source: Optional[source_mod.MarketplaceSource] = None,
    force: bool = False,
    lang: str = "",
) -> List[dict]:
    """Return the bundles listed by the catalog index.

    The index carries only ``{id, name, path}`` per bundle; the items are
    read when the user actually opens one, so a page render never downloads
    every bundle record.
    """
    transport = source or source_mod.default_source()
    try:
        index = transport.fetch_index(force=force)
    except Exception:
        return []
    bundles = index.get("bundles") if isinstance(index, dict) else None
    if not isinstance(bundles, (list, tuple)):
        return []
    result: List[dict] = []
    for raw in bundles:
        if isinstance(raw, str) and raw.strip():
            result.append({"id": raw.strip(), "name": raw.strip(), "path": ""})
        elif isinstance(raw, dict) and str(raw.get("id") or "").strip():
            result.append({
                "id": str(raw["id"]).strip(),
                "name": _localized_name(raw.get("name"), lang) or str(raw["id"]).strip(),
                "path": str(raw.get("path") or "").strip(),
            })
    return result


def load_bundle(
    bundle_id: str,
    source: Optional[source_mod.MarketplaceSource] = None,
) -> dict:
    """Fetch one catalog bundle record and validate its member list."""
    from core.marketplace.bundles import BundleError, _clean_member

    transport = source or source_mod.default_source()
    record = transport.fetch_bundle(bundle_id)
    items = []
    for raw in record.get("items") or []:
        member = _clean_member(raw)
        if member is not None:
            items.append(member)
    if not items:
        raise BundleError(
            "empty_bundle",
            f"Bundle '{bundle_id}' declares no usable items",
            {"id": bundle_id},
        )
    return {
        "id": str(record.get("id") or bundle_id),
        "name": str(record.get("name") or bundle_id),
        "version": str(record.get("version") or ""),
        "items": items,
    }


def plan_bundle(
    bundle_id: str,
    source: Optional[source_mod.MarketplaceSource] = None,
    lang: str = "",
) -> dict:
    """Return the plan of a catalog-level bundle.

    The bundle record is turned into the same ``recommends`` shape a card
    uses, so one planner serves both entry points.
    """
    from core.marketplace.bundles import plan

    record = load_bundle(bundle_id, source)
    carrier = {"id": record["id"], "bundle": record["items"]}
    plan_data = plan(carrier, source, lang)
    plan_data["bundle"] = {
        "id": record["id"],
        "name": _localized_name(record.get("name"), lang) or record["id"],
        "version": record["version"],
    }
    return plan_data


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
