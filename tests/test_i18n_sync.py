# -*- coding: utf-8 -*-
"""tests/test_i18n_sync.py - переводы интерфейса синхронизированы с кодом.

Каждый ключ вида t('...') / t("..."), использованный в ui/ и core/, должен
присутствовать во ВСЕХ языковых файлах (канонические defaults/langs/ и
легаси-копии langs/). Это закрывает класс «молчаливых» fallback-переводов:
при отсутствии ключа t() возвращает сам ключ, и пользователь видит
технический идентификатор вместо текста.
"""
import json
import os
import re

_LANG_RELS = (
    "defaults/langs/en.json",
    "defaults/langs/ru.json",
    "defaults/langs/zh-CN.json",
    "langs/en.json",
    "langs/ru.json",
    "langs/zh-CN.json",
)
_SOURCE_DIRS = ("ui", "core")
_CALL_RE = re.compile(r"\bt\(\s*['\"]([A-Za-z0-9_.-]+)['\"]")


def _repo_root() -> str:
    """Абсолютный путь к корню репозитория (родитель tests/)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _collect_keys() -> set:
    """Собрать все литеральные ключи t('...') из ui/ и core/."""
    root = _repo_root()
    keys = set()
    for sub in _SOURCE_DIRS:
        base = os.path.join(root, sub)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                if fn.startswith("._"):
                    # AppleDouble sidecar files (OneDrive/Mac) are binary and
                    # must never be read as UTF-8 source.
                    continue
                with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                    for line in f:
                        if line.lstrip().startswith("#"):
                            continue
                        keys.update(_CALL_RE.findall(line))
    return keys


def _load_lang(rel: str) -> dict:
    with open(os.path.join(_repo_root(), rel), encoding="utf-8") as f:
        return json.load(f)


def test_t_keys_exist_in_every_lang_file():
    """Все ключи t(...) из кода есть во всех шести языковых файлах."""
    keys = _collect_keys()
    assert keys, "regexp не нашёл ни одного t(...) ключа - паттерн сломан?"
    errors = []
    for rel in _LANG_RELS:
        data = _load_lang(rel)
        missing = sorted(k for k in keys if k not in data)
        if missing:
            errors.append(f"{rel}: нет {len(missing)} ключей: {missing[:20]}")
    assert not errors, "\n".join(errors)


def test_lang_files_are_valid_json():
    """Все языковые файлы - валидные JSON-объекты."""
    for rel in _LANG_RELS:
        assert isinstance(_load_lang(rel), dict), f"{rel}: не JSON-объект"
