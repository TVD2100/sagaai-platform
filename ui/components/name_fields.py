# -*- coding: utf-8 -*-
"""
ui.components.name_fields - the "Names" block of an entity form.

One entity carries four name slots: a user-defined name and three
interface-language translations (English, Russian, Simplified Chinese).
The block renders all four inputs and returns the values as a dict ready
for a save call.

Resolution rules live in core.naming; this module only collects the text.
The streamlit module is a parameter rather than a module-level import: the
orchestrator page resolves ``st`` late, and a test may swap the module for
a double, so the block must render through whatever object the caller holds.
"""
from typing import Any, Dict, Optional

import streamlit

from core.i18n import t
from core.naming import NAME_FIELDS, slots_error

#: Interface-language label of each translation input. Full literal keys,
#: so the translation-coverage check sees them in the source.
_LABEL_KEYS = {
    "en": "names_label_en",
    "ru": "names_label_ru",
    "zh": "names_label_zh",
}


# Widget keys are formed from the owner key (an assistant id, a slug, or the
# literal "new"), so two forms on one page never share an input.
def name_widget_key(owner: str, slot: str) -> str:
    """Return the stable widget key of one name input."""
    return f"name_field_{owner}_{slot}"


def stored_name_slots(record: Dict[str, Any]) -> Dict[str, str]:
    """Return the four name slots of a stored record, ready for the form.

    The plain ``name`` of a read view already carries a translation, so the
    user-defined input is filled from ``name_user`` whenever it exists.
    """
    record = record or {}
    slots = {"name": str(record.get("name_user") or record.get("name") or "")}
    for field in NAME_FIELDS.values():
        slots[field] = str(record.get(field) or "")
    return slots


def form_name_error(form: Dict[str, Any], subject: str = "assistant") -> str:
    """Return the refusal message when a form carries no name at all."""
    slots = {key: str((form or {}).get(key) or "").strip()
             for key in ("name",) + tuple(NAME_FIELDS.values())}
    return slots_error(slots, subject)


def form_name_kwargs(form: Dict[str, Any]) -> Dict[str, str]:
    """Return the translation arguments of a save call.

    An empty input means "this slot is not set" and is passed as ``None``, so
    the repository stores an empty translation instead of a blank string.
    """
    return {field: (form or {}).get(field) or None for field in NAME_FIELDS.values()}


def render_name_fields(
    lang: str,
    owner: str,
    values: Optional[Dict[str, Any]] = None,
    *,
    st: Any = None,
    show_header: bool = True,
    user_key: Optional[str] = None,
) -> Dict[str, str]:
    """Render the four name inputs and return the entered values.

    ``values`` is the record being edited (or an empty dict while creating).
    The plain ``name`` value of a record is its user-defined slot: the data
    layer resolves a display name, so the editor must write that slot back.

    ``user_key`` overrides the widget key of the user-defined input; a form
    that existed before translations keeps its historical key, so no saved
    widget state is lost on upgrade.

    Returns a dict with the keys ``name``, ``name_en``, ``name_ru`` and
    ``name_zh``; every value is stripped of surrounding whitespace and an
    empty string means "this slot is not set".
    """
    st = st if st is not None else streamlit
    values = values or {}
    if show_header:
        st.markdown(t("names_section", lang=lang))
        st.caption(t("names_section_help", lang=lang))

    user_name = st.text_input(
        t("names_user_label", lang=lang),
        value=str(values.get("name_user") or values.get("name") or ""),
        key=user_key or name_widget_key(owner, "user"),
        help=t("names_user_help", lang=lang),
    )

    translations: Dict[str, str] = {}
    columns = st.columns(len(NAME_FIELDS))
    for column, (code, field) in zip(columns, NAME_FIELDS.items()):
        with column:
            translations[field] = st.text_input(
                t(_LABEL_KEYS[code], lang=lang),
                value=str(values.get(field) or ""),
                key=name_widget_key(owner, code),
                help=t("names_translation_help", lang=lang),
            )

    result = {"name": str(user_name or "").strip()}
    for field in NAME_FIELDS.values():
        result[field] = str(translations.get(field) or "").strip()
    return result


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
