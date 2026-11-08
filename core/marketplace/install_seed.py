# -*- coding: utf-8 -*-
"""
core.marketplace.install_seed - what an installed agent starts with.

An agent installed from a catalog runs on the platform like a bundled one.
Two things make that true once the package is read and its update merge is
settled: the platform basis fills the model keys the package never named, so
the employee can run and its chat page can open, and the catalog card fills
the name translations of a package that merely repeats the card's English
name. Both steps only fill gaps - a value the package declares always wins.

The catalog defaults and the ``marketplace`` marker are merged first, by
:func:`core.marketplace.agent_package._apply_config_defaults`, so the basis
never reads a bundle that has not yet recorded where it came from.
"""
from __future__ import annotations

from typing import Any

from core.builtin_config_defaults import builtin_config_defaults
from core.marketplace.agent_package import _apply_config_defaults
from core.naming import name_fields_from

#: Name slots a catalog card may fill on an installed agent.
NAME_SLOTS = ("name_en", "name_ru", "name_zh")


def _truthy(value: Any) -> bool:
    """Return whether *value* means "on" in a stored config."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _seed_platform_config(bundle: dict) -> dict:
    """Fill config keys the package left out from the platform defaults.

    An agent installed from a catalog runs on the platform like a bundled
    one, so it receives the same starting config.

    The search pair follows the agent's own choice. An agent that never
    enabled chat web search gets no search model - a provider name without
    the switch would look configured while the tool stays absent.

    Parameters:
        bundle: the package payload; its ``config`` mapping is extended in
            place and returned.

    Returns:
        The same bundle with the platform defaults filled in.
    """
    config = dict(bundle.get("config") or {})
    defaults = builtin_config_defaults()
    web_search = _truthy(config.get("chat_web_search"))
    for key, value in defaults.items():
        if key.startswith("search_") and not web_search:
            continue
        config.setdefault(key, value)
    bundle["config"] = config
    return bundle


def _apply_entity_names(bundle: dict, entity: dict) -> dict:
    """Move the catalog card translations onto an installed agent's names.

    A package often carries a bare English name, while the catalog card
    describes the same agent in every language. When the package name is the
    card's English name, the package simply repeats what the catalog already
    says: the translations then fill the slots and the interface shows the
    name in its own language.

    A package whose bare name differs from the card's English name carries
    the author's own wording. That wording is the user-defined slot and is
    left alone, translations included.

    Parameters:
        bundle: the package payload; its name slots are extended.
        entity: parsed ``entity.json`` of the catalog entry.

    Returns:
        The same bundle with the name slots the card may fill.
    """
    package_fields = name_fields_from(bundle)
    package_name = package_fields.get("name_user") or ""
    if not package_name:
        # The package already describes itself with translations only.
        return bundle
    card_fields = name_fields_from(entity or {})
    card_en = card_fields.get("name_en") or ""
    if not card_en or package_name.strip().casefold() != card_en.strip().casefold():
        return bundle
    bundle["name"] = ""
    for field in NAME_SLOTS:
        bundle[field] = card_fields.get(field) or ""
    return bundle


def prepare_install_bundle(
    bundle: dict,
    entity: dict,
    entity_id: str,
    version: str,
    channel: str,
) -> dict:
    """Return the bundle an install hands to the importer.

    The steps run in order: catalog defaults and the ``marketplace`` marker
    first, then the platform basis, then the card translations - a name is
    only replaced after the config has settled, so no step reads a
    half-built bundle.

    Parameters:
        bundle: the package payload after the update merge.
        entity: parsed ``entity.json`` of the catalog entry.
        entity_id: catalog id of the entity being installed.
        version: version being installed.
        channel: release channel name.

    Returns:
        The bundle ready for the orchestrator importer.
    """
    bundle = _apply_config_defaults(bundle, entity, entity_id, version, channel)
    bundle = _seed_platform_config(bundle)
    return _apply_entity_names(bundle, entity)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
