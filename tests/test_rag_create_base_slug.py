# -*- coding: utf-8 -*-
"""tests/test_rag_create_base_slug.py - explicit slug support in create_base.

``rag.create_base`` derives the slug from the display name via ``_slugify``;
for non-Latin names (e.g. Cyrillic "Маркетинг") that derivation degrades to
the fallback ``base``. The new optional ``slug`` parameter lets callers pass
a stable Latin slug explicitly; it is normalised to the safe character set
([a-z0-9_.-], lowercase) and raises ValueError when nothing usable remains.

Covers: name-derived behaviour unchanged, explicit slug used verbatim
(after normalisation), unsafe characters folded, Cyrillic-only slug
declined, duplicate explicit slug declined, empty/whitespace slug falls
back to the name.
"""
import importlib
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR isolating RAG bases."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_rag_create_slug_")
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    from tests._test_isolation import isolated_app_modules as _iso_app_modules
    with _iso_app_modules():
        import core.paths as paths_mod  # noqa: F401
        importlib.reload(paths_mod)
        import storage.db as db_mod
        importlib.reload(db_mod)
        db_mod.reset_engine()
        db_mod.reset_devagent_engine()

        yield tmp

        db_mod.reset_engine()
        db_mod.reset_devagent_engine()

    if old_env:
        os.environ["SAGAAI_DATA_DIR"] = old_env
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)
    shutil.rmtree(tmp, ignore_errors=True)


def test_name_derived_slug_unchanged(isolated_data_dir):
    """Without an explicit slug the name-derived behaviour is preserved."""
    import core.rag as rag
    base = rag.create_base(name="My KB", provider="YandexAI")
    assert base["slug"] == "my_kb"


def test_explicit_slug_is_used(isolated_data_dir):
    """An explicit Latin slug lands in the manifest verbatim."""
    import core.rag as rag
    base = rag.create_base(name="Маркетинг", provider="YandexAI",
                           slug="marketing")
    assert base["slug"] == "marketing"
    assert base["name"] == "Маркетинг"
    assert os.path.isfile(os.path.join(rag.base_dir("marketing"),
                                       "manifest.json"))


def test_explicit_slug_is_normalised(isolated_data_dir):
    """Unsafe characters are folded; the result is lowercase."""
    import core.rag as rag
    base = rag.create_base(name="KB", provider="YandexAI",
                           slug="My-KB.v2!")
    assert base["slug"] == "my-kb.v2"


def test_cyrillic_only_slug_raises(isolated_data_dir):
    """A slug with no Latin characters raises instead of a silent fallback."""
    import core.rag as rag
    with pytest.raises(ValueError):
        rag.create_base(name="Книга", provider="YandexAI", slug="маркетинг")


def test_duplicate_explicit_slug_raises(isolated_data_dir):
    """An explicit slug that already exists is declined."""
    import core.rag as rag
    rag.create_base(name="KB1", provider="YandexAI", slug="dup_kb")
    with pytest.raises(ValueError):
        rag.create_base(name="KB2", provider="YandexAI", slug="dup_kb")


def test_blank_slug_falls_back_to_name(isolated_data_dir):
    """An empty or whitespace slug keeps the name-derived path."""
    import core.rag as rag
    base = rag.create_base(name="Fallback KB", provider="YandexAI",
                           slug="   ")
    assert base["slug"] == "fallback_kb"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
