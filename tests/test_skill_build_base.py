# -*- coding: utf-8 -*-
"""tests/test_skill_build_base.py - RAG Base Creator helper (bm25 mode).

The bundled skill script defaults/skills/rag_base_creator/scripts/build_base.py
must build a base with NO embedding calls in the default bm25 mode: chunks are
inserted into the local index and BM25 structures are built lazily by
core.rag_bm25, so no YandexAI keys are required.

Covers: dry-run plan (search_mode visible, nothing created) and a full bm25
build (skill build -> search_base hit via BM25) with embed_text monkeypatched
to fail on any call.
"""
import importlib
import importlib.util
import os
import sys
import tempfile
import shutil
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL_SCRIPT = (_REPO_ROOT / "defaults" / "skills" / "rag_base_creator"
                 / "scripts" / "build_base.py")


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR isolating RAG bases and the DB."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_skill_build_")
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    from tests._test_isolation import isolated_app_modules as _iso_app_modules
    with _iso_app_modules():
        import core.paths as paths_mod
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


def _load_skill_module():
    """Import build_base.py as a standalone module (fresh copy)."""
    name = "skill_build_base"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _SKILL_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _source_dir(root: Path) -> Path:
    """Create a tiny two-file source folder inside *root*."""
    src = root / "docs"
    src.mkdir(parents=True, exist_ok=True)
    (src / "guide.md").write_text(
        "# Installation guide\n\nInstall the platform with pip and run the wizard.\n",
        encoding="utf-8",
    )
    (src / "faq.md").write_text(
        "# FAQ\n\nHow do I reindex a knowledge base? Use the storage page.\n",
        encoding="utf-8",
    )
    return src


def _args(src: Path, **overrides) -> Namespace:
    """Build a Namespace matching build_base.py's argparse surface."""
    base = dict(
        install_root=str(_REPO_ROOT),
        source=[str(src)],
        name="Skill KB",
        description="",
        slug="skill_kb",
        search_mode="bm25",
        chunk_size=1800,
        chunk_overlap=0,
        exclude=[],
        extensions=[],
        limit=None,
        slots=[],
        dry_run=False,
        no_embed=False,
        replace=False,
    )
    base.update(overrides)
    return Namespace(**base)


def test_dry_run_reports_bm25_and_creates_nothing(isolated_data_dir, tmp_path):
    """--dry-run shows the search_mode and creates no base."""
    mod = _load_skill_module()
    src = _source_dir(tmp_path)
    result = mod.run(_args(src, dry_run=True))
    plan = result["plan"]
    assert result.get("dry_run") is True
    assert plan["search_mode"] == "bm25"
    assert plan["files_total"] == 2
    assert plan["chunks_total"] >= 2
    from core import rag
    assert rag.list_bases(with_stats=False) == []


def test_bm25_build_needs_no_embeddings(isolated_data_dir, tmp_path):
    """Full bm25 build: no embed_text calls; base is searchable by keywords."""
    import core.rag_embeddings as re_mod

    def _fail(*a, **k):
        raise AssertionError("embed_text must not be called in bm25 mode")

    old = re_mod.embed_text
    re_mod.embed_text = _fail
    try:
        mod = _load_skill_module()
        src = _source_dir(tmp_path)
        result = mod.run(_args(src))
    finally:
        re_mod.embed_text = old

    assert result["ok"] is True
    assert result["chunks"] >= 2
    assert result["status"] == "ready"

    from core import rag
    base = rag.get_base("skill_kb")
    assert base["search_mode"] == "bm25"
    page = rag.list_chunks("skill_kb", limit=10)
    assert page["chunks"]
    assert all(not c.get("has_embedding") for c in page["chunks"])

    from core.rag_search import search_base
    hits = search_base("skill_kb", "wizard")
    assert hits, "BM25 must find the chunk containing 'wizard'"
    assert hits[0]["source"] == "guide.md"
    assert hits[0].get("mode") == "bm25"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
