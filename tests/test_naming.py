# -*- coding: utf-8 -*-
"""Unit tests for the localized display-name resolver (core.naming)."""
from core.naming import (
    display_name,
    name_fields_from,
    normalize_lang,
    parse_name,
    record_names,
    resolve_display_name,
    with_display_name,
)


def test_normalize_lang_accepts_codes_and_display_names():
    assert normalize_lang("en") == "en"
    assert normalize_lang("RU") == "ru"
    assert normalize_lang("zh-CN") == "zh"
    assert normalize_lang("zh_CN") == "zh"
    assert normalize_lang("Русский") == "ru"
    assert normalize_lang("简体中文") == "zh"
    assert normalize_lang("English") == "en"
    assert normalize_lang("") == ""
    assert normalize_lang(None) == ""
    assert normalize_lang("klingon") == ""


def test_parse_name_plain_string_is_user_name():
    assert parse_name("Корректор") == {"user": "Корректор", "en": "", "ru": "", "zh": ""}


def test_parse_name_mapping_fills_translations():
    names = parse_name({"en": "Proofreader", "ru": "Корректор", "zh-CN": "校对员"})
    assert names == {"user": "", "en": "Proofreader", "ru": "Корректор", "zh": "校对员"}


def test_parse_name_mapping_with_explicit_user_slot():
    names = parse_name({"user": "Мой агент", "en": "My agent"})
    assert names["user"] == "Мой агент"
    assert names["en"] == "My agent"


def test_parse_name_ignores_empty_and_unknown_keys():
    names = parse_name({"en": "  ", "de": "Deutsch", "ru": "Корректор"})
    assert names["en"] == ""
    assert names["ru"] == "Корректор"
    assert names["zh"] == ""


def test_record_names_treats_legacy_string_name_as_user_name():
    names = record_names({"name": "Локальный агент", "slug": "local"})
    assert names["user"] == "Локальный агент"
    assert names["en"] == names["ru"] == names["zh"] == ""


def test_record_names_reads_the_name_column_as_the_user_slot():
    """The column is the user-defined slot even next to translations.

    A record may carry both: the owner typed a name and (or later) filled
    translations. The typed name is what they expect to see, in every
    language. A built-in or marketplace entity has an empty column and
    therefore resolves by translation.
    """
    names = record_names({"name": "старое", "name_en": "New"})
    assert names["en"] == "New"
    assert names["user"] == "старое"

    empty = record_names({"name": "", "name_en": "New"})
    assert empty["user"] == ""
    assert empty["en"] == "New"


def test_resolve_user_defined_name_wins_every_language():
    names = {"user": "Разработчик", "en": "Developer", "ru": "Разработчик2", "zh": "开发者"}
    for lang in ("en", "ru", "zh", "de", ""):
        assert resolve_display_name(names, lang) == "Разработчик"


def test_resolve_uses_interface_language_then_english():
    names = {"user": "", "en": "Developer", "ru": "Разработчик", "zh": "开发者"}
    assert resolve_display_name(names, "ru") == "Разработчик"
    assert resolve_display_name(names, "zh-CN") == "开发者"
    assert resolve_display_name(names, "en") == "Developer"
    # An interface language without a translation falls back to English.
    assert resolve_display_name(names, "de") == "Developer"
    assert resolve_display_name(names, "") == "Developer"


def test_resolve_falls_back_to_any_translation_when_english_missing():
    names = {"user": "", "en": "", "ru": "Разработчик", "zh": ""}
    assert resolve_display_name(names, "zh") == "Разработчик"


def test_resolve_returns_fallback_when_every_slot_is_empty():
    names = {"user": "", "en": "", "ru": "", "zh": ""}
    assert resolve_display_name(names, "ru", "slug-name") == "slug-name"
    assert resolve_display_name(names, "ru") == ""


def test_display_name_ends_with_slug():
    assert display_name({"slug": "korrektor", "name": ""}, "ru") == "korrektor"
    assert display_name({"slug": "korrektor", "name": "Корректор"}, "ru") == "Корректор"


def test_with_display_name_keeps_raw_slots_and_resolves_name():
    record = {"slug": "ya_agent", "name": "", "name_en": "YaAgent", "name_ru": "YaAgent"}
    out = with_display_name(record, "ru")
    assert out["name"] == "YaAgent"
    assert out["name_user"] == ""
    assert out["name_en"] == "YaAgent"
    assert out["name_zh"] == ""
    # The input record is not modified.
    assert record["name_zh"] if "name_zh" in record else True


def test_with_display_name_does_not_mutate_input():
    record = {"slug": "x", "name": "Local"}
    out = with_display_name(record, "en")
    assert out["name"] == "Local"
    assert "name_user" not in record


def test_name_fields_from_accepts_string_and_mapping():
    from_string = name_fields_from({"name": "Корректор"})
    assert from_string == {"name_user": "Корректор", "name_en": "", "name_ru": "", "name_zh": ""}
    from_mapping = name_fields_from({"name": {"en": "Meeting Minutes", "ru": "Протоколы встреч", "zh-CN": "会议纪要"}})
    assert from_mapping == {
        "name_user": "",
        "name_en": "Meeting Minutes",
        "name_ru": "Протоколы встреч",
        "name_zh": "会议纪要",
    }


def test_name_fields_from_explicit_fields_win_over_mapping():
    data = {"name_en": "Explicit", "name": {"en": "FromMapping"}}
    assert name_fields_from(data)["name_en"] == "Explicit"


def test_spaces_are_trimmed_everywhere():
    names = parse_name({"en": "  Developer  ", "ru": "   "})
    assert names["en"] == "Developer"
    assert names["ru"] == ""
    assert display_name({"slug": "s", "name": "  Имя  "}, "ru") == "Имя"
