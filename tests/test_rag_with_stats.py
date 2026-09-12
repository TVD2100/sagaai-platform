# -*- coding: utf-8 -*-
"""
tests/test_rag_with_stats.py - with_stats opt-out for RAG hot paths.

``list_bases`` / ``get_base`` / ``list_bases_with_activity`` accept a
``with_stats`` flag. With ``with_stats=False`` the per-base SQLite index is
never opened and no ``index_stats`` block is attached, so hot paths
(orchestrator prompts, UI option lists, RAG tool invocations) never trigger
index I/O. The default (``with_stats=True``) keeps the previous behaviour.
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
    tmp = tempfile.mkdtemp(prefix="sagaai_test_rag_with_stats_")
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


def _make_base(isolated_data_dir):
    """Create a base via the public API and return (rag module, slug)."""
    import core.rag as rag
    base = rag.create_base(name="KB", description="", provider="YandexAI",
                           embedding_model="text-search-doc")
    return rag, base["slug"]


def _forbid_index_io(monkeypatch):
    """Patch core.rag_index so ANY attempt to open/scan an index fails."""
    import core.rag_index as rag_index
    opened = []

    def _fail(path, *args, **kwargs):
        opened.append(path)
        raise AssertionError("index.db must not be opened")

    monkeypatch.setattr(rag_index, "_connect", _fail)
    monkeypatch.setattr(rag_index, "index_stats", _fail)
    return opened


class TestWithStatsOptOut:
    def test_list_bases_without_stats_pure(self, isolated_data_dir, monkeypatch):
        """list_bases(with_stats=False) reads manifests without index I/O."""
        rag, slug = _make_base(isolated_data_dir)
        opened = _forbid_index_io(monkeypatch)

        bases = rag.list_bases(with_stats=False)
        assert [b["slug"] for b in bases] == [slug]
        assert all("index_stats" not in b for b in bases)
        assert opened == []

    def test_get_base_without_stats_pure(self, isolated_data_dir, monkeypatch):
        """get_base(slug, with_stats=False) never opens the SQLite index."""
        rag, slug = _make_base(isolated_data_dir)
        opened = _forbid_index_io(monkeypatch)

        data = rag.get_base(slug, with_stats=False)
        assert data["slug"] == slug
        assert "index_stats" not in data
        assert opened == []

    def test_default_with_stats_true_still_works(self, isolated_data_dir):
        """The default (with_stats=True) keeps attaching index_stats."""
        rag, slug = _make_base(isolated_data_dir)
        data = rag.get_base(slug)
        assert data["index_stats"]["chunks"] == 0
        bases = rag.list_bases()
        assert bases[0]["index_stats"]["chunks"] == 0

    def test_list_bases_with_activity_without_stats(
            self, isolated_data_dir, monkeypatch):
        """list_bases_with_activity(with_stats=False) is pure metadata."""
        rag, slug = _make_base(isolated_data_dir)
        opened = _forbid_index_io(monkeypatch)

        bases = rag.list_bases_with_activity(with_stats=False)
        assert [b["slug"] for b in bases] == [slug]
        assert "active" in bases[0]
        assert "index_stats" not in bases[0]
        assert opened == []

    def test_activity_default_keeps_stats(self, isolated_data_dir):
        """list_bases_with_activity() default keeps index_stats."""
        rag, slug = _make_base(isolated_data_dir)
        bases = rag.list_bases_with_activity()
        assert bases[0]["index_stats"]["chunks"] == 0

    def test_activity_loads_config_once(self, isolated_data_dir, monkeypatch):
        """Config/services are loaded once per call, not once per base."""
        rag, first_slug = _make_base(isolated_data_dir)
        rag.create_base(name="KB2", provider="YandexAI",
                        embedding_model="text-search-doc")

        import core.config as config_mod
        calls = []
        original = config_mod.load_config

        def counting_load():
            calls.append(1)
            return original()

        monkeypatch.setattr(config_mod, "load_config", counting_load)
        bases = rag.list_bases_with_activity(with_stats=False)
        assert len(bases) == 2
        assert len(calls) == 1
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
