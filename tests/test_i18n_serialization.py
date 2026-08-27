# -*- coding: utf-8 -*-
"""tests/test_i18n_serialization.py - языковые JSON сериализуются стабильно.

Класс дефекта из пост-мортема: порядок ключей в языковых файлах зависел от
неупорядоченных промежуточных контейнеров, из-за чего одинаковые по смыслу
изменения давали разные файлы (шумные diff/backup). dumps_lang()/
dump_lang_file() гарантируют детерминированный порядок: порядок вставки
по умолчанию и алфавитный при sort_keys=True.
"""
import json
import os
import sys
import pathlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.i18n import dumps_lang, dump_lang_file


def test_dumps_lang_preserves_insertion_order():
    """По умолчанию порядок ключей == порядку вставки в dict."""
    data = {"zeta": "Z", "alpha": "А", "middle": "М"}
    text = dumps_lang(data)
    assert text.endswith("\n")
    parsed = json.loads(text)
    assert list(parsed.keys()) == ["zeta", "alpha", "middle"]
    assert parsed == data
    # повторный вызов детерминирован
    assert dumps_lang(data) == text


def test_dumps_lang_sort_keys_is_alphabetical():
    """sort_keys=True выдаёт алфавитный порядок независимо от вставки."""
    data = {"zeta": "Z", "alpha": "А", "middle": "М"}
    parsed = json.loads(dumps_lang(data, sort_keys=True))
    assert list(parsed.keys()) == ["alpha", "middle", "zeta"]


def test_dumps_lang_keeps_unicode_chars():
    """Русские символы не экранируются в \\u-последовательности."""
    text = dumps_lang({"hello": "Привет", "zh": "你好"})
    assert "Привет" in text and "你好" in text
    assert "\\u" not in text


def test_dump_lang_file_writes_and_roundtrips(tmp_path):
    """dump_lang_file пишет файл с сохранением порядка ключей и без потерь."""
    target = tmp_path / "ru.json"
    data = {"b": "Б", "a": "А", "lang_display_name": "Тест"}
    assert dump_lang_file(str(target), data) is True
    raw = target.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw) == data
    assert list(json.loads(raw).keys()) == ["b", "a", "lang_display_name"]


def test_dump_lang_file_creates_parent_dirs(tmp_path):
    """Целевые поддиректории создаются автоматически."""
    target = tmp_path / "nested" / "sub" / "en.json"
    assert dump_lang_file(str(target), {"hi": "Hi"}) is True
    assert target.read_text(encoding="utf-8").endswith("\n")


def test_dump_lang_file_returns_false_on_write_error(tmp_path):
    """Ошибка записи даёт False, а не исключение."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    target = blocker / "lang.json"  # parent - файл, makedirs упадёт
    assert dump_lang_file(str(target), {"k": "v"}) is False
