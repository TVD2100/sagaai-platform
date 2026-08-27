# -*- coding: utf-8 -*-
"""
UI regression tests for the read-only RAG storage page.

Verifies that the redesigned storage page:
  - has NO create-base form / file uploader / indexing / settings edit UI,
  - still deletes a whole base with confirmation,
  - renders the chunk management section (search, expanders, edit/delete buttons).
"""
import importlib
import os
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR isolating RAG bases and the DB.

    Uses the shared tests._test_isolation.isolated_app_modules helper so
    import-time constants (RAG_BASES_DIR) always come from the temp DATA_DIR
    and stale package attributes never leak between tests.
    """
    tmp = tempfile.mkdtemp(prefix="sagaai_test_storage_ui_")
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


def _make_base_with_chunks(data_dir: str) -> dict:
    """Create a ready base with two chunks and return its manifest dict."""
    from core import rag
    from core.rag_index import add_chunk, create_index_db
    base = rag.create_base(
        name="Docs KB", description="desc", provider="YandexAI",
        embedding_model="text-search-doc", chunk_size=500, chunk_overlap=20,
    )
    slug = base["slug"]
    db = rag.index_db_path(slug)
    create_index_db(db, dimension=2, provider="YandexAI",
                    embedding_model="text-search-doc")
    add_chunk(db, "Hello world chunk", source="one.md", chunk_index=0,
              vector=[1.0, 0.0])
    add_chunk(db, "Second fragment", source="two.md", chunk_index=0,
              vector=[0.0, 1.0])
    rag.set_status(slug, "ready")
    return base


def _fresh_storage_page():
    """Import the storage page; app modules are already isolated by the
    fixture, so the import is always fresh and picks up the temp DATA_DIR."""
    return importlib.import_module("ui.pages.storage")


def _render(st, page, **session):
    st.session_state.update(dict(ui_lang="English", **session))
    try:
        page.page_storage()
    except StopRerun:
        pass


def _call_names(st):
    return [c[0] for c in st.calls]


def _button_keys(st):
    return [c[2].get("key") for c in st.calls if c[0] in ("button", "form_submit_button")]


def test_page_has_no_create_or_indexing_ui(isolated_data_dir):
    """The storage page must not render creation/indexing/upload controls."""
    _make_base_with_chunks(isolated_data_dir)
    with install_streamlit_mock() as st:
        page = _fresh_storage_page()
        _render(st, page)
        names = _call_names(st)
        assert "file_uploader" not in names
        assert "number_input" not in names
        assert "form" not in names  # no create form at all
        keys = _button_keys(st)
        assert "storage_new" not in keys
        assert not any(str(k or "").startswith("storage_idx_") for k in keys)
        assert not any(str(k or "").startswith("storage_ed_name_") for k in keys)


def test_page_without_bases_shows_empty_hint(isolated_data_dir):
    """Empty state renders no create button."""
    with install_streamlit_mock() as st:
        page = _fresh_storage_page()
        _render(st, page)
        assert "storage_new" not in _button_keys(st)


def test_base_card_actions_and_chunks_section(isolated_data_dir):
    """The base card exposes search/delete and the chunk manager section."""
    base = _make_base_with_chunks(isolated_data_dir)
    slug = base["slug"]
    with install_streamlit_mock() as st:
        page = _fresh_storage_page()
        _render(st, page, **{f"storage_show_chunks_{slug}": True})
        keys = _button_keys(st)
        # Whole-base deletion remains available
        assert f"storage_del_{slug}" in keys
        # Test-search button remains available
        assert f"storage_ts_{slug}" in keys
        # Chunk section rendered: search input + per-chunk edit/delete buttons
        text_inputs = [c[2].get("key") for c in st.calls if c[0] == "text_input"]
        assert f"storage_cq_{slug}" in text_inputs
        assert any(str(k or "").startswith(f"storage_ce_{slug}_") for k in keys)
        assert any(str(k or "").startswith(f"storage_cd_{slug}_") for k in keys)
        # No create/edit/upload/index widgets
        names = _call_names(st)
        assert "file_uploader" not in names
        assert "storage_new" not in keys
