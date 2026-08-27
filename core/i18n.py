"""
core.i18n - internationalisation helpers.
No streamlit imports; lang is passed as a parameter instead of reading st.session_state.
Uses functools.lru_cache instead of @st.cache_data.

Default language files live in defaults/langs/; the legacy langs/ folder
remains a fallback for installations created before the defaults/ layout.
Files from the legacy folder are used only for names not present in defaults/.

Translation fallback policy:
- English (en.json) is the canonical reference language and must contain all keys.
- When a key is missing in the requested language, ``t()`` falls back to the
  English translation automatically.
- If the key is also missing in English, the raw key string is returned.

Serialisation policy: language JSON files must be written with a stable,
repeatable key order via dumps_lang()/dump_lang_file(). The default keeps
the dict insertion order; sort_keys=True switches to alphabetical order.
This keeps language-file diffs deterministic across sessions instead of
depending on unordered intermediate containers.
"""
import os
import json
from functools import lru_cache
from core.paths import LANGS_DIR


def _lang_dirs() -> list:
    """Return (defaults_langs_dir, legacy_langs_dir) when they exist."""
    dirs = []
    try:
        from core import defaults
        if os.path.isdir(defaults.langs_dir()):
            dirs.append(defaults.langs_dir())
    except Exception:
        pass
    if os.path.isdir(LANGS_DIR):
        dirs.append(LANGS_DIR)
    return dirs


def discover_langs() -> dict:
    """
    Scan the default and legacy language directories for *.json files.
    Returns {display_name: file_path}. Defaults/ wins on name conflicts.
    """
    result: dict = {}
    for directory in reversed(_lang_dirs()):
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(directory, fname)
            try:
                with open(fpath, encoding="utf-8") as f:
                    data = json.load(f)
                display = data.get("lang_display_name", fname.replace(".json", ""))
                result[display] = fpath
            except Exception:
                pass
    return result


def get_langs() -> dict:
    """Cached wrapper around discover_langs (TTL-less, invalidated by process restart)."""
    return _cached_langs()


def invalidate_langs_cache() -> None:
    """
    Clear the cached language list so the next call to get_langs() rescans
    the language directories. Call this after adding/removing/renaming a
    .json file inside defaults/langs/ or langs/.
    """
    _cached_langs.cache_clear()


@lru_cache(maxsize=1)
def _cached_langs() -> dict:
    return discover_langs()


@lru_cache(maxsize=32)
def load_lang_data(fpath: str) -> dict:
    """Load and cache a language JSON file by path."""
    try:
        with open(fpath, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def t(key: str, lang: str = None, **kwargs) -> str:
    """
    Translate *key* using language *lang* (display name).
    Falls back to the first available language, then to English (en.json),
    then to *key* itself.
    UI layer should pass the current language explicitly.
    """
    langs = get_langs()
    if not langs:
        return key.format(**kwargs) if kwargs else key
    # Resolve file path for the requested language
    if lang and lang in langs:
        fpath = langs[lang]
    else:
        fpath = next(iter(langs.values()), "")
    if not fpath:
        return key.format(**kwargs) if kwargs else key
    text = load_lang_data(fpath).get(key)
    if text is not None:
        return text.format(**kwargs) if kwargs else text
    # Fallback to English
    en_path = langs.get("English")
    if en_path and en_path != fpath:
        en_text = load_lang_data(en_path).get(key)
        if en_text is not None:
            return en_text.format(**kwargs) if kwargs else en_text
    return key.format(**kwargs) if kwargs else key


def dumps_lang(data: dict, sort_keys: bool = False) -> str:
    """Serialize a language dict to stable JSON text.

    Returns a newline-terminated, UTF-8-safe JSON string (ensure_ascii=False,
    indent=2). Keys are emitted in dict insertion order, or alphabetically
    when sort_keys=True. The trailing newline keeps language-file diffs clean.
    """
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n"


def dump_lang_file(fpath: str, data: dict, sort_keys: bool = False) -> bool:
    """Write *data* to a language JSON file with a stable key order.

    Creates parent directories when needed. Returns True on success and
    False on any write error.
    """
    try:
        os.makedirs(os.path.dirname(fpath) or ".", exist_ok=True)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(dumps_lang(data, sort_keys=sort_keys))
        return True
    except Exception:
        return False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
