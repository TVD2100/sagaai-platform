"""
core.fs — filesystem IO helpers.
No streamlit imports; errors are raised or returned silently (no st.error).
"""
import os
import json
from pathlib import Path


def ensure_dir(path: str) -> str:
    """Create directory (and parents) if it does not exist, return path."""
    Path(path).mkdir(parents=True, exist_ok=True)
    return path


def read_json_file(path: str, default):
    """Read and parse a JSON file; return *default* on any error or absence."""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def write_json_file(path: str, data) -> bool:
    """Serialise *data* as JSON to *path*; return True on success."""
    try:
        ensure_dir(os.path.dirname(path) or ".")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def read_text_file(path: str, default: str = "") -> str:
    """Read a UTF-8 text file; return *default* on any error or absence."""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return default


def write_text_file(path: str, text: str) -> bool:
    """Write *text* as UTF-8 to *path*; return True on success."""
    try:
        ensure_dir(os.path.dirname(path) or ".")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except Exception:
        return False


def decode_bytes(raw: bytes, encodings=None) -> str:
    """Decode *raw* bytes trying multiple encodings; fall back to replace mode."""
    if encodings is None:
        encodings = ("utf-8", "cp1251", "latin-1")
    for enc in encodings:
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def combine_nonempty(parts: list, sep: str = "\n\n") -> str:
    """Join non-empty strings from *parts* with *sep*."""
    return sep.join(p for p in parts if p)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
