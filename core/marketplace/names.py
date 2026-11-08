# -*- coding: utf-8 -*-
"""
core.marketplace.names - the display name of a catalog entity.

The marketplace describes an entity with one name slot per language (and
optionally a user-defined one). Resolution follows :mod:`core.naming`, so a
catalog card, the installed entity and the interface always pick the same
text for the same language.
"""
from typing import Any, Dict, Optional


def entity_name(entity: Dict[str, Any], lang: Optional[str] = None) -> str:
    """Return the human name of *entity* for *lang*.

    A bare string is the user-defined name and wins in every language; a name
    mapping is resolved per language with English as the fallback. An entity
    with no usable name answers its id, so a card never shows a blank title.
    """
    from core.naming import display_name

    entity = entity or {}
    tail = str(entity.get("id") or "skill")
    fields = {key: value for key, value in entity.items() if key.startswith("name_")}
    fields["name"] = entity.get("name")
    return display_name(fields, lang, tail) or tail


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
