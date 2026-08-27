"""
test_phase1_core_pure.py — verifies that core modules import cleanly (no streamlit),
that fs utilities work correctly (round-trip), render functions produce expected output,
and i18n.t returns the key when no translation is available.
"""
import os
import sys
import json
import importlib
import subprocess
import tempfile
import py_compile
import pytest

# Ensure the sagaai package is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── Syntax check for all new .py files ──────────────────────────────────────

SAGAAI_DIR = os.path.join(os.path.dirname(__file__), "..")

NEW_PYTHON_FILES = [
    "core/__init__.py",
    "core/paths.py",
    "core/fs.py",
    "core/i18n.py",
    "core/services.py",
    "core/config.py",
    "core/skills.py",
    "core/threads.py",
    "core/files.py",
    "core/api_layer.py",
    "core/render.py",
    "storage/__init__.py",
    "storage/models.py",
    "storage/db.py",
    "storage/repository.py",
    "app.py",
    "ui/__init__.py",
    "ui/app.py",
    "ui/pages/__init__.py",
    "ui/pages/chat.py",
    "ui/pages/skills.py",
    "ui/pages/settings.py",
    "ui/pages/history.py",
]


@pytest.mark.parametrize("rel_path", NEW_PYTHON_FILES)
def test_py_compile(rel_path):
    """Every new .py file must compile without syntax errors."""
    full_path = os.path.join(SAGAAI_DIR, rel_path)
    # py_compile raises SyntaxError / py_compile.PyCompileError on bad syntax
    py_compile.compile(full_path, doraise=True)


# ─── core imports without streamlit ──────────────────────────────────────────

CORE_MODULES = [
    "core.paths",
    "core.fs",
    "core.i18n",
    "core.services",
    "core.config",
    "core.skills",
    "core.threads",
    "core.files",
    "core.render",
]


@pytest.mark.parametrize("module_name", CORE_MODULES)
def test_core_imports_without_streamlit(module_name):
    """
    Each core module must be importable in a subprocess where streamlit is absent.
    This is the definitive test that no core/* module depends on streamlit at import time.
    """
    script = f"""
import sys, os
sys.path.insert(0, {repr(SAGAAI_DIR)})

# Block streamlit from being importable
import types

class StreamlitBlocker:
    def find_spec(self, name, *args, **kwargs):
        if name == 'streamlit' or name.startswith('streamlit.'):
            raise ImportError(f'streamlit is blocked: {{name}}')
        return None

sys.meta_path.insert(0, StreamlitBlocker())

import {module_name}
print('OK')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"Module {module_name!r} failed to import without streamlit.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "OK"


# ─── fs round-trip ────────────────────────────────────────────────────────────

def test_fs_json_roundtrip(tmp_path):
    """write_json_file / read_json_file round-trip."""
    from core.fs import write_json_file, read_json_file
    data = {"key": "value", "num": 42, "list": [1, 2, 3]}
    path = str(tmp_path / "test.json")
    assert write_json_file(path, data) is True
    loaded = read_json_file(path, {})
    assert loaded == data


def test_fs_json_missing_returns_default(tmp_path):
    """read_json_file returns default when file doesn't exist."""
    from core.fs import read_json_file
    loaded = read_json_file(str(tmp_path / "nope.json"), "default_val")
    assert loaded == "default_val"


def test_fs_text_roundtrip(tmp_path):
    """write_text_file / read_text_file round-trip."""
    from core.fs import write_text_file, read_text_file
    text = "Hello, мир!\nLine 2\n"
    path = str(tmp_path / "test.txt")
    assert write_text_file(path, text) is True
    loaded = read_text_file(path, "")
    assert loaded == text


def test_fs_text_missing_returns_default(tmp_path):
    """read_text_file returns default when file doesn't exist."""
    from core.fs import read_text_file
    assert read_text_file(str(tmp_path / "absent.txt"), "fallback") == "fallback"


def test_fs_ensure_dir(tmp_path):
    """ensure_dir creates nested directories and returns the path."""
    from core.fs import ensure_dir
    nested = str(tmp_path / "a" / "b" / "c")
    result = ensure_dir(nested)
    assert os.path.isdir(nested)
    assert result == nested


# ─── decode_bytes ─────────────────────────────────────────────────────────────

def test_decode_bytes_utf8():
    from core.fs import decode_bytes
    assert decode_bytes("hello".encode("utf-8")) == "hello"


def test_decode_bytes_cp1251():
    from core.fs import decode_bytes
    text = "Привет"
    raw  = text.encode("cp1251")
    assert decode_bytes(raw) == text


def test_decode_bytes_fallback():
    from core.fs import decode_bytes
    # raw bytes that are invalid in utf-8 and cp1251 but survive latin-1
    raw = bytes([0x80, 0x81])
    result = decode_bytes(raw)
    assert isinstance(result, str)
    assert len(result) > 0


# ─── combine_nonempty ─────────────────────────────────────────────────────────

def test_combine_nonempty_basic():
    from core.fs import combine_nonempty
    assert combine_nonempty(["a", "b", "c"]) == "a\n\nb\n\nc"


def test_combine_nonempty_skips_empty():
    from core.fs import combine_nonempty
    assert combine_nonempty(["a", "", "c", None, "d"]) == "a\n\nc\n\nd"


def test_combine_nonempty_custom_sep():
    from core.fs import combine_nonempty
    assert combine_nonempty(["x", "y"], sep=" | ") == "x | y"


def test_combine_nonempty_all_empty():
    from core.fs import combine_nonempty
    assert combine_nonempty(["", "", ""]) == ""


# ─── render ───────────────────────────────────────────────────────────────────

def test_md_to_txt_basic():
    """_md_to_txt strips markdown and returns plain text."""
    from core.render import _md_to_txt
    md   = "# Heading\n\nSome **bold** text and `code`."
    txt  = _md_to_txt(md)
    assert txt          != ""
    assert "#"          not in txt
    assert "**"         not in txt
    assert "`"          not in txt
    assert "Heading"    in txt
    assert "bold"       in txt
    assert "code"       in txt


def test_md_to_txt_removes_links():
    from core.render import _md_to_txt
    md  = "Visit [Google](https://google.com) for more."
    txt = _md_to_txt(md)
    assert "Google"         in txt
    assert "https://"       not in txt


def test_md_to_txt_removes_code_blocks():
    from core.render import _md_to_txt
    md  = "Before\n```python\nx = 1\n```\nAfter"
    txt = _md_to_txt(md)
    assert "Before"  in txt
    assert "After"   in txt
    assert "```"     not in txt


def test_md_to_html_nonempty():
    """_md_to_html returns a non-empty HTML string."""
    from core.render import _md_to_html
    result = _md_to_html("# Hello\n\nWorld")
    assert result.strip() != ""
    assert "<html" in result.lower()
    assert "Hello" in result


def test_md_to_html_contains_body():
    from core.render import _md_to_html
    result = _md_to_html("**Bold** text")
    assert "<body>" in result or "<body" in result


# ─── i18n ─────────────────────────────────────────────────────────────────────

def test_i18n_t_returns_key_when_no_langs(monkeypatch):
    """t(key) returns key when no language files are found."""
    import core.i18n as i18n_mod
    # Monkeypatch get_langs to return empty dict
    monkeypatch.setattr(i18n_mod, "get_langs", lambda: {})
    assert i18n_mod.t("some_key") == "some_key"


def test_i18n_t_returns_key_when_translation_missing(tmp_path, monkeypatch):
    """t(key) returns key when key is not in the language file."""
    import core.i18n as i18n_mod
    # Create a minimal language file
    lang_file = tmp_path / "en.json"
    lang_file.write_text(json.dumps({"lang_display_name": "English", "hello": "Hello"}),
                         encoding="utf-8")
    monkeypatch.setattr(i18n_mod, "get_langs", lambda: {"English": str(lang_file)})
    # Clear lru_cache to pick up new mock
    i18n_mod.load_lang_data.cache_clear()
    result = i18n_mod.t("nonexistent_key", lang="English")
    assert result == "nonexistent_key"


def test_i18n_t_returns_translation(tmp_path, monkeypatch):
    """t(key, lang=...) returns the translated string."""
    import core.i18n as i18n_mod
    lang_file = tmp_path / "en.json"
    lang_file.write_text(
        json.dumps({"lang_display_name": "English", "greeting": "Hello, {name}!"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(i18n_mod, "get_langs", lambda: {"English": str(lang_file)})
    i18n_mod.load_lang_data.cache_clear()
    result = i18n_mod.t("greeting", lang="English", name="World")
    assert result == "Hello, World!"


def test_i18n_t_no_lang_param_uses_first(tmp_path, monkeypatch):
    """t(key) without lang falls back to first available language."""
    import core.i18n as i18n_mod
    lang_file = tmp_path / "ru.json"
    lang_file.write_text(
        json.dumps({"lang_display_name": "Русский", "hello": "Привет"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(i18n_mod, "get_langs", lambda: {"Русский": str(lang_file)})
    i18n_mod.load_lang_data.cache_clear()
    assert i18n_mod.t("hello") == "Привет"


# ─── UI layer: syntax only (streamlit not installed) ─────────────────────────

UI_FILES = [
    "ui/__init__.py",
    "ui/app.py",
    "ui/pages/__init__.py",
    "ui/pages/chat.py",
    "ui/pages/skills.py",
    "ui/pages/settings.py",
    "ui/pages/history.py",
]


@pytest.mark.parametrize("rel_path", UI_FILES)
def test_ui_syntax(rel_path):
    """UI files must have valid Python syntax (streamlit not installed is fine)."""
    full_path = os.path.join(SAGAAI_DIR, rel_path)
    py_compile.compile(full_path, doraise=True)
