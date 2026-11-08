# -*- coding: utf-8 -*-
"""core.naming_backfill - turn a pre-translation name into the four slots.

An entity installed from the bundled defaults before localized names existed
carries one string in the user-defined slot. That string is not the owner's
choice: the platform shipped it. Left alone, the entity would keep showing
one frozen name in every language and, worse, would look like something the
user had renamed on purpose.

This module recognises the shipped names and produces the fields to write
back: the shipped string becomes a translation and the user-defined slot is
cleared, so the interface follows its language again. A name the owner
deliberately changed is never touched - the caller passes the names a preset
used to ship, and anything else counts as the user's own edit.

An entity created locally by the owner needs no backfill at all: its name
sits in the user-defined slot, which is exactly where the resolver looks
first, so it keeps winning in every language (see core.naming).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

#: Names the bundled entities carried before translations existed, by slug.
#:
#: Only these exact strings are recognised as "shipped by the platform". A
#: record holding anything else is a user-renamed entity and keeps its name:
#: the owner's choice always outranks a preset.
SHIPPED_NAMES: Dict[str, Tuple[str, ...]] = {
    "dev_agent": ("DevAgent",),
    "task_scout": ("Task Scout", "Разведчик задач", "任务侦察员"),
    "universal_agent": ("Universal-Agent", "Universal Agent", "Универсальный агент", "通用智能体"),
    "ya_agent": ("YaAgent",),
    "korrektor": ("Корректор", "Proofreader", "校对员"),
    "universalnyy_tyutor": ("Универсальный тьютор", "Universal Tutor", "通用导师"),
}

_SLOT_FIELDS = ("name_en", "name_ru", "name_zh")


def shipped_names_for(slug: str, preset: Dict[str, Any] = None) -> Tuple[str, ...]:
    """Return every string that counts as a platform-provided name.

    The historical table is the authority; the preset's own translations are
    added so a record that already holds one of them is still recognised as
    unmodified (an installation that imported the new files but lost the
    translations, for instance).
    """
    known = list(SHIPPED_NAMES.get(str(slug or ""), ()))
    preset = preset or {}
    for key in ("name_user",) + _SLOT_FIELDS:
        text = str(preset.get(key) or "").strip()
        if text and text not in known:
            known.append(text)
    return tuple(known)


def default_name_backfill(current: Dict[str, Any], preset: Dict[str, Any],
                         shipped: Iterable[str] = ()) -> Dict[str, str]:
    """Return the name fields to write for a preset-installed entity.

    *current* is the stored record, *preset* is the shipped description of
    the same entity and *shipped* lists the names the preset used to carry.

    Returns a kwargs dict (``name``, ``name_en``, ``name_ru``, ``name_zh``)
    ready for the repository update, or an empty dict when nothing should
    change. The function is pure - it never writes.

    Rules, in order:

    1. A record that already carries a translation is on the new scheme (or
       the owner filled one): leave it alone.
    2. An empty record takes the preset's slots.
    3. A record whose only name is a shipped one is migrated: the shipped
       string becomes a translation and the user-defined slot is cleared.
    4. Any other name belongs to the owner: leave it alone.
    """
    current = current or {}
    preset = preset or {}

    target = {
        "name": str(preset.get("name_user") or "").strip(),
        "name_en": str(preset.get("name_en") or "").strip(),
        "name_ru": str(preset.get("name_ru") or "").strip(),
        "name_zh": str(preset.get("name_zh") or "").strip(),
    }
    if not any(target.values()):
        return {}

    if any(str(current.get(field) or "").strip() for field in _SLOT_FIELDS):
        return {}

    stored_user = str(current.get("name_user") or "").strip()
    stored_name = str(current.get("name") or "").strip()
    candidate = stored_user or stored_name

    if not candidate:
        return target

    known = {str(text or "").strip() for text in shipped}
    known.update(str(text or "").strip() for text in target.values())
    known.discard("")
    if candidate not in known:
        return {}
    return target


def builtin_name_backfill(current: Dict[str, Any], slug: str = "dev_agent") -> Dict[str, str]:
    """Return the name kwargs that migrate the built-in DevAgent to the slots.

    The preset is the shipped description: an empty user-defined slot and the
    three translations. A record the owner renamed keeps its name (see
    :func:`default_name_backfill`).
    """
    from core.naming import DEVAGENT_NAME_EN, DEVAGENT_NAME_RU, DEVAGENT_NAME_ZH

    preset = {
        "name_user": "",
        "name_en": DEVAGENT_NAME_EN,
        "name_ru": DEVAGENT_NAME_RU,
        "name_zh": DEVAGENT_NAME_ZH,
    }
    try:
        return default_name_backfill(current, preset, shipped_names_for(slug, preset))
    except Exception:
        return {}


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
