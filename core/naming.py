# -*- coding: utf-8 -*-
"""
core.naming - localized display names for orchestrators and assistants.

Every entity carries four name slots:

  * the user-defined name - free text, in any language, entered by the owner;
  * English, Russian and Chinese translations.

Resolution for interface language L is ordered:

  1. the user-defined name, when non-empty;
  2. the translation for L;
  3. the English translation;
  4. any other non-empty translation;
  5. the slug, so a label is never blank.

Rule 1 is what makes a user-created entity look identical in every locale:
its owner typed one name, and that name is the fallback for all languages.
An entity installed from the marketplace carries no user-defined name and
shows the translation of the interface language, falling back to English.

A stored name value is either a plain string or a language-to-text mapping:

  * a plain string is the user-defined name (how a locally created entity
    is stored, and how every entity was stored before translations);
  * a mapping holds the translations (how the marketplace and the bundled
    defaults describe an entity).

This module is pure: no streamlit, no storage import. The rules are thus
unit-testable and reusable by the data layer, the UI and the marketplace.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

#: Canonical translation keys, in the order the interface shows them.
NAME_LANGS = ("en", "ru", "zh")

#: Storage field that carries each translation on a record.
NAME_FIELDS = {"en": "name_en", "ru": "name_ru", "zh": "name_zh"}

#: Every spelling a caller may use for a language code.
LANG_ALIASES = {
    "en": "en", "en-us": "en", "en_us": "en", "english": "en",
    "ru": "ru", "ru-ru": "ru", "ru_ru": "ru", "russian": "ru",
    "русский": "ru", "ru-RU": "ru",
    "zh": "zh", "zh-cn": "zh", "zh_cn": "zh", "zh-hans": "zh",
    "zh-hant": "zh", "chinese": "zh", "简体中文": "zh", "中文": "zh",
}

#: Interface language display names (the value of ``lang_display_name``).
DISPLAY_LANG_CODES = {
    "english": "en",
    "русский": "ru",
    "简体中文": "zh",
}


def normalize_lang(lang: Optional[str]) -> str:
    """Return the canonical code (en / ru / zh) for a language hint.

    Accepts a bare code, a regional form (zh-CN, ru_RU), an English language
    name or the display name of an interface language file. Returns an empty
    string when the hint names no language we ship a translation for.
    """
    raw = str(lang or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    code = LANG_ALIASES.get(low) or LANG_ALIASES.get(raw)
    if code:
        return code
    return DISPLAY_LANG_CODES.get(low, "")


def parse_name(value: Any) -> Dict[str, str]:
    """Split a stored name value into the four slots.

    A plain string fills the user-defined slot only - that is the
    user-created scenario and the shape every entity had before
    translations existed. A mapping fills the translation slots; a mapping
    that also carries an explicit ``user`` (or ``custom``/``name``) key
    fills the user-defined slot from it.

    Returns a dict with the keys ``user``, ``en``, ``ru`` and ``zh``, each
    a possibly empty string.
    """
    out: Dict[str, str] = {"user": "", "en": "", "ru": "", "zh": ""}
    if isinstance(value, dict):
        for key, val in value.items():
            text = str(val or "").strip()
            if not text:
                continue
            code = normalize_lang(key)
            if not code and str(key).strip().lower() in ("user", "custom", "name"):
                code = "user"
            if code and not out.get(code):
                out[code] = text
        return out
    text = str(value or "").strip()
    if text:
        out["user"] = text
    return out


def record_names(record: Dict[str, Any]) -> Dict[str, str]:
    """Return the four name slots of an entity record.

    The dedicated fields win. A plain ``name`` string is the user-defined
    slot: that is what the column holds for a locally created entity and what
    every entity held before translations existed. The one exception is an
    already-resolved view - it carries an explicit ``name_user`` key, which
    then decides and keeps re-resolution idempotent.
    """
    record = record or {}
    names: Dict[str, str] = {"user": str(record.get("name_user") or "").strip()}
    for code, field in NAME_FIELDS.items():
        names[code] = str(record.get(field) or "").strip()
    legacy = record.get("name")
    if isinstance(legacy, dict):
        for code, text in parse_name(legacy).items():
            if text and not names.get(code):
                names[code] = text
        return names
    text = str(legacy or "").strip()
    if text and "name_user" not in record and not names["user"]:
        names["user"] = text
    return names


def resolve_display_name(names: Dict[str, str], lang: Optional[str] = None,
                         fallback: str = "") -> str:
    """Pick the display name from the four slots for the interface language."""
    names = names or {}
    user = str(names.get("user") or "").strip()
    if user:
        return user
    order = []
    code = normalize_lang(lang)
    if code:
        order.append(code)
    for candidate in NAME_LANGS:
        if candidate not in order:
            order.append(candidate)
    for candidate in order:
        text = str(names.get(candidate) or "").strip()
        if text:
            return text
    return str(fallback or "").strip()


def display_name(record: Dict[str, Any], lang: Optional[str] = None,
                 fallback: str = "") -> str:
    """Return the display name of an entity record for *lang*.

    The fallback chain ends with the record slug, so a label is never empty.
    """
    record = record or {}
    tail = fallback or str(record.get("slug") or record.get("id") or "")
    return resolve_display_name(record_names(record), lang, tail)


def with_display_name(record: Dict[str, Any], lang: Optional[str] = None) -> Dict[str, Any]:
    """Return a copy of *record* whose ``name`` is resolved for *lang*.

    The raw slots travel next to it as ``name_user``, ``name_en``,
    ``name_ru`` and ``name_zh``, so an edit form can prefill the four fields
    without a second read. Every legacy reader of ``record['name']`` (thread
    snapshots, prompt lists, sorting) keeps working and sees a sensible
    string; persistence keeps using the raw repository records.
    """
    if not isinstance(record, dict):
        return record
    names = record_names(record)
    out = dict(record)
    tail = str(record.get("slug") or record.get("id") or "")
    out["name"] = resolve_display_name(names, lang, tail)
    out["name_user"] = names["user"]
    for code, field in NAME_FIELDS.items():
        out[field] = names[code]
    return out


def name_fields_from(data: Dict[str, Any]) -> Dict[str, str]:
    """Return the four storage fields for an import or export dict.

    Accepts a bare string ``name``, a mapping ``name`` and explicit
    ``name_user`` / ``name_en`` / ``name_ru`` / ``name_zh`` keys. All four
    fields are returned, empty ones included, so a round trip through a
    folder file is deterministic.
    """
    data = data or {}
    names = parse_name(data.get("name"))
    out: Dict[str, str] = {
        "name_user": str(data.get("name_user") or "").strip(),
    }
    for code, field in NAME_FIELDS.items():
        out[field] = str(data.get(field) or "").strip()
    for slot, text in names.items():
        if not text:
            continue
        key = "name_user" if slot == "user" else NAME_FIELDS[slot]
        if not out.get(key):
            out[key] = text
    return out
#: Localized names of the built-in DevAgent. The user-defined slot stays
#: empty: nobody typed that name, so the interface follows its language
#: instead of freezing one string.
DEVAGENT_NAME_EN = "Developer"
DEVAGENT_NAME_RU = "Разработчик"
DEVAGENT_NAME_ZH = "开发者"

#: The three translations as repository keyword arguments.
DEVAGENT_NAME_FIELDS = {
    "name_en": DEVAGENT_NAME_EN,
    "name_ru": DEVAGENT_NAME_RU,
    "name_zh": DEVAGENT_NAME_ZH,
}

#: Every key that carries a name slot in a tool call or a stored record.
NAME_SLOT_KEYS = ("name", "name_en", "name_ru", "name_zh")


def localized_text(value: Any, lang: Optional[str] = None) -> str:
    """Return the best text for *lang* from a string or a name mapping.

    A manifest that ships a bare string is accepted as-is. A mapping goes
    through the same rules as a stored entity, so a catalog card and the
    installed entity it installs pick the same text; a key outside the three
    shipped languages still carries text and is shown rather than dropped.
    """
    if not isinstance(value, dict):
        return str(value or "").strip()
    text = resolve_display_name(parse_name(value), lang)
    if text:
        return text
    for candidate in value.values():
        if str(candidate or "").strip():
            return str(candidate).strip()
    return ""


def split_name_slots(name: Optional[str], name_en: Optional[str] = None,
                     name_ru: Optional[str] = None,
                     name_zh: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    """Return the stripped user-defined name and the three translations."""
    user = str(name or "").strip()
    translations: Dict[str, str] = {}
    for field, value in (("name_en", name_en), ("name_ru", name_ru),
                         ("name_zh", name_zh)):
        translations[field] = str(value or "").strip()
    return user, translations


def merge_untouched(installed: Any, package: Any, previous: Any) -> Any:
    """Return the package value when the user never changed the installed one.

    A value equal to what the installed package shipped is a value the user
    left alone, so an update may move it. Any other value is the user's own
    and stays.
    """
    if installed == previous:
        return package
    return installed


def merge_name_slots(bundle: Dict[str, Any], installed_row: Dict[str, Any],
                     previous: Dict[str, Any]) -> Dict[str, Any]:
    """Return *bundle* with the name slots an update is allowed to move.

    A translation follows the package while the user has not changed it; an
    empty slot is a translation the installed version did not ship, not a
    choice of the owner. The user-defined name is the owner's own wording and
    stays unless it was merely the name the installed package shipped.
    """
    installed_row = installed_row or {}
    previous = previous or {}
    for field in NAME_FIELDS.values():
        bundle[field] = merge_untouched(
            str(installed_row.get(field) or ""),
            str(bundle.get(field) or ""),
            str(previous.get(field) or ""),
        )
    previous_name = previous.get("name")
    if isinstance(previous_name, dict):
        previous_name = ""
    bundle["name"] = merge_untouched(
        str(installed_row.get("name_user") or installed_row.get("name") or ""),
        str(bundle.get("name") or ""),
        str(previous_name or ""),
    )
    return bundle


def bundle_name_fields(record: Dict[str, Any]) -> Dict[str, str]:
    """Return the four name slots a folder bundle carries for a record."""
    names = record_names(record)
    out: Dict[str, str] = {"name": names["user"]}
    for code, field in NAME_FIELDS.items():
        out[field] = names[code]
    return out


def names_from_bundle(bundle: Dict[str, Any], slug: str = "") -> Dict[str, str]:
    """Return the repository name fields for an imported bundle.

    An empty user-defined name is a real state: an entity described by
    translations only shows the translation of the interface language. Only a
    bundle with no name information at all falls back to its slug.
    """
    fields = name_fields_from(bundle)
    if not any(fields.values()):
        return {"name": slug, "name_en": "", "name_ru": "", "name_zh": ""}
    return {"name": fields["name_user"], "name_en": fields["name_en"],
            "name_ru": fields["name_ru"], "name_zh": fields["name_zh"]}


def merge_slots(current: Dict[str, Any], name: Optional[str] = None,
                name_en: Optional[str] = None, name_ru: Optional[str] = None,
                name_zh: Optional[str] = None) -> Dict[str, str]:
    """Return the four raw slots after an edit.

    A slot passed as ``None`` keeps its stored value; an explicitly passed
    empty string clears it. The user-defined slot falls back to ``name_user``
    - never to the resolved ``name`` of a read view, which already carries a
    translation.
    """
    current = current or {}
    user = current.get("name_user") if name is None else name
    if name is None and "name_user" not in current:
        user = current.get("name", "")
    slots: Dict[str, str] = {"name": str(user or "").strip()}
    for field, value in (("name_en", name_en), ("name_ru", name_ru),
                         ("name_zh", name_zh)):
        if value is None:
            value = current.get(field)
        slots[field] = str(value or "").strip()
    return slots


def slots_error(slots: Dict[str, str], subject: str) -> str:
    """Return the refusal message for an edit that strips every name."""
    if any(str(value or "").strip() for value in (slots or {}).values()):
        return ""
    return ("The %s would be left without a name: keep the user slot or at "
            "least one translation non-empty." % subject)


def name_update_error(current: Dict[str, Any], updates: Dict[str, Any]) -> str:
    """Return a refusal message when *updates* would strip every name."""
    slots: Dict[str, str] = {}
    for key in NAME_SLOT_KEYS:
        source = "name_user" if key == "name" else key
        value = (updates or {}).get(key)
        if value is None:
            value = (current or {}).get(source)
        slots[key] = str(value or "").strip()
    return slots_error(slots, "employee")


def apply_meta_names(bundle: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    """Fill the four name slots of *bundle* from a package metadata dict.

    A package may carry a bare name (the user-defined one), a name mapping or
    the explicit translation keys; ``name_fields_from`` accepts all shapes.
    """
    fields = name_fields_from(meta)
    bundle["name"] = fields.get("name_user") or ""
    for field in NAME_FIELDS.values():
        bundle[field] = fields.get(field) or ""
    return bundle


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
