# -*- coding: utf-8 -*-
"""
tests.test_default_rag_bases - preset RAG bases and auto-assignment.

Covers:
  - core.defaults helpers (rag_bases_dir, list_default_rag_base_slugs,
    load_default_rag_base),
  - core.default_imports.ensure_default_rag_bases(): seeding from
    defaults/rag_bases, idempotency, and the removed-defaults protection
    recorded by core.rag.delete_base(),
  - the catalog base is never copied back from its bundled folder,
  - preset orchestrator rag_bases: a fresh bundled orchestrator gets the
    preset base assigned, an existing one gets it via union (backfill)
    without losing user settings, and the base appears in the built prompt
    metadata.

The YaAgent base moved to the catalog, so the seeding mechanics are
exercised on a synthetic bundled base created for the duration of a test.
"""
import importlib
import json
import os
import shutil
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


CATALOG_RAG_SLUG = "yaagentai_2020"
PRESET_SLUG = "test_preset_rag"
BASE_SLUG = "test_preset_base"


def _bundled_base_folder(slug: str) -> Path:
    """Return the bundled folder path of a preset base."""
    from core import defaults

    return Path(defaults.rag_bases_dir()) / slug


def _write_bundled_base(slug: str = BASE_SLUG) -> Path:
    """Create a synthetic bundled RAG base for one test."""
    folder = _bundled_base_folder(slug)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps({
        "slug": slug,
        "name": "Synthetic preset base",
        "description": "",
        "status": "ready",
        "chunk_size": 1800,
        "chunk_overlap": 0,
    }, ensure_ascii=False), encoding="utf-8")
    return folder


def _remove_bundled_base(slug: str = BASE_SLUG) -> None:
    """Drop a folder created by :func:`_write_bundled_base`."""
    shutil.rmtree(str(_bundled_base_folder(slug)), ignore_errors=True)


def _orch_folder(slug: str) -> Path:
    """Return the bundled folder path of a preset orchestrator."""
    from core import defaults

    return Path(defaults.orchestrators_dir()) / slug


def _write_bundled_orchestrator(slug: str, rag_bases: list) -> Path:
    """Create a synthetic bundled orchestrator with preset rag_bases."""
    folder = _orch_folder(slug)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "orchestrator.json").write_text(json.dumps({
        "format": "sagaai_orchestrator/v1",
        "slug": slug,
        "name": slug.replace("_", " ").title(),
        "description": "",
        "prompt_text": "You are a bundled test agent.",
        "config": {
            "strong_service": "DeepSeek",
            "strong_model": "deepseek-flash",
            "rag_bases": list(rag_bases),
        },
        "tools": [],
        "max_steps": 50,
        "auto_apply": True,
    }, ensure_ascii=False), encoding="utf-8")
    return folder


def _remove_bundled_orchestrator(slug: str) -> None:
    """Drop a folder created by :func:`_write_bundled_orchestrator`."""
    shutil.rmtree(str(_orch_folder(slug)), ignore_errors=True)


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR isolating the preset import from real data."""
    import tempfile

    from tests._test_isolation import isolated_app_modules as _iso_app_modules

    tmp = tempfile.mkdtemp(prefix="sagaai_test_default_rag_")
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

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


# ─── core.defaults helpers ────────────────────────────────────────────────────

class TestDefaultsRagHelpers:
    def test_helpers_see_bundled_base(self):
        """The bundled folder stays readable even though it is catalog-only."""
        from core import defaults

        slugs = defaults.list_default_rag_base_slugs()
        assert CATALOG_RAG_SLUG in slugs
        data = defaults.load_default_rag_base(CATALOG_RAG_SLUG)
        assert data is not None
        assert data.get("slug") == CATALOG_RAG_SLUG


# ─── default import of RAG bases ──────────────────────────────────────────────

class TestDefaultRagImport:
    def test_first_run_creates_runtime_base_with_source(self, isolated_data_dir):
        from core.default_imports import ensure_default_rag_bases
        from core import rag

        _write_bundled_base()
        try:
            result = ensure_default_rag_bases()
        finally:
            _remove_bundled_base()
        assert result.get(BASE_SLUG) == "created", result

        fresh = rag._load_manifest_raw(BASE_SLUG)
        assert fresh["slug"] == BASE_SLUG
        assert fresh["source"] == f"defaults/rag_bases/{BASE_SLUG}"

        import core.paths as paths_mod
        assert os.path.isfile(
            os.path.join(paths_mod.RAG_BASES_DIR, BASE_SLUG, "manifest.json")
        )

    def test_the_catalog_base_is_not_seeded(self, isolated_data_dir):
        """A base moved to the catalog arrives from there, not from defaults."""
        from core.default_imports import ensure_default_rag_bases
        from core import rag

        result = ensure_default_rag_bases()

        assert CATALOG_RAG_SLUG not in result
        assert rag.get_base(CATALOG_RAG_SLUG, with_stats=False) == {}

    def test_import_is_idempotent(self, isolated_data_dir):
        from core.default_imports import ensure_default_rag_bases

        _write_bundled_base()
        try:
            assert ensure_default_rag_bases().get(BASE_SLUG) == "created"
            assert ensure_default_rag_bases().get(BASE_SLUG) == "exists"
        finally:
            _remove_bundled_base()

    def test_existing_base_is_not_overwritten(self, isolated_data_dir):
        from core import rag
        from core.default_imports import ensure_default_rag_bases

        _write_bundled_base()
        try:
            assert ensure_default_rag_bases().get(BASE_SLUG) == "created"
            assert rag.update_base(
                BASE_SLUG,
                {"name": "Моя пользовательская база", "description": "UPDATED"},
            )["name"] == "Моя пользовательская база"

            assert ensure_default_rag_bases().get(BASE_SLUG) == "exists"
            data = rag.get_base(BASE_SLUG)
            assert data["name"] == "Моя пользовательская база"
            assert data["description"] == "UPDATED"
        finally:
            _remove_bundled_base()

    def test_deleted_default_base_is_not_resurrected(self, isolated_data_dir):
        from core import rag
        from core.default_imports import ensure_default_rag_bases

        _write_bundled_base()
        try:
            assert ensure_default_rag_bases().get(BASE_SLUG) == "created"
            assert rag.delete_base(BASE_SLUG) is True
            assert rag.get_base(BASE_SLUG) == {}

            # The removed marker must prevent the next import from recreating it.
            assert ensure_default_rag_bases().get(BASE_SLUG) == "exists"
            assert rag.get_base(BASE_SLUG) == {}
        finally:
            _remove_bundled_base()


# ─── preset orchestrator auto-assignment ──────────────────────────────────────

class TestPresetRagAssignment:
    def test_fresh_preset_gets_base_assigned(self, isolated_data_dir):
        from core import default_imports

        _write_bundled_orchestrator(PRESET_SLUG, [BASE_SLUG])
        try:
            result = default_imports.ensure_all_defaults()
            from core.orchestrators import get_orchestrator

            assert result["orchestrators"].get(PRESET_SLUG) == "created", result
            cfg = get_orchestrator(PRESET_SLUG)["config"]
        finally:
            _remove_bundled_orchestrator(PRESET_SLUG)
        assert cfg.get("rag_bases") == [BASE_SLUG]

    def test_backfill_existing_orchestrator_without_rag_bases(
        self, isolated_data_dir
    ):
        from core import default_imports

        _write_bundled_orchestrator(PRESET_SLUG, [BASE_SLUG])
        try:
            result = default_imports.ensure_all_defaults()
            assert result["orchestrators"].get(PRESET_SLUG) == "created", result

            from core.orchestrators import get_orchestrator, save_orchestrator

            cfg = dict(get_orchestrator(PRESET_SLUG)["config"])
            # Simulate a pre-preset installation: user settings without rag_bases.
            cfg.pop("rag_bases", None)
            cfg["strong_temperature"] = 0.123
            save_orchestrator(PRESET_SLUG, config=cfg)

            assert default_imports.ensure_all_defaults()["orchestrators"].get(
                PRESET_SLUG
            ) == "exists"
            cfg2 = get_orchestrator(PRESET_SLUG)["config"]
        finally:
            _remove_bundled_orchestrator(PRESET_SLUG)
        assert cfg2["rag_bases"] == [BASE_SLUG]
        assert cfg2["strong_temperature"] == 0.123

    def test_explicit_user_assignment_is_preserved(self, isolated_data_dir):
        from core import default_imports

        _write_bundled_orchestrator(PRESET_SLUG, [BASE_SLUG])
        try:
            default_imports.ensure_all_defaults()

            from core.orchestrators import get_orchestrator, save_orchestrator

            cfg = dict(get_orchestrator(PRESET_SLUG)["config"])
            cfg["rag_bases"] = ["custom_user_base"]
            save_orchestrator(PRESET_SLUG, config=cfg)

            default_imports.ensure_all_defaults()
            cfg2 = get_orchestrator(PRESET_SLUG)["config"]
        finally:
            _remove_bundled_orchestrator(PRESET_SLUG)
        assert cfg2["rag_bases"] == ["custom_user_base"]

    def test_flags_listed_in_metadata_block(self, isolated_data_dir):
        from core import default_imports

        _write_bundled_base()
        _write_bundled_orchestrator(PRESET_SLUG, [BASE_SLUG])
        try:
            # The prompt block lists the bases that actually exist at run
            # time, so the preset base has to be seeded first.
            assert default_imports.ensure_all_defaults()["rag_bases"].get(
                BASE_SLUG
            ) == "created"

            from core.orchestrators import build_assistant_dicts

            strong, _weak = build_assistant_dicts(PRESET_SLUG)
            prompt = strong.get("text") or ""
        finally:
            _remove_bundled_orchestrator(PRESET_SLUG)
            _remove_bundled_base()
        assert "Available RAG knowledge bases" in prompt
        assert BASE_SLUG in prompt

    def test_flag_can_be_removed_by_user(self, isolated_data_dir):
        from core import default_imports

        _write_bundled_orchestrator(PRESET_SLUG, [BASE_SLUG])
        try:
            default_imports.ensure_all_defaults()

            from core.orchestrators import (
                get_orchestrator,
                save_orchestrator,
                get_orchestrator_rag_bases,
            )

            cfg = dict(get_orchestrator(PRESET_SLUG)["config"])
            cfg["rag_bases"] = []
            save_orchestrator(PRESET_SLUG, config=cfg)

            default_imports.ensure_all_defaults()
            bases = get_orchestrator_rag_bases(PRESET_SLUG)
        finally:
            _remove_bundled_orchestrator(PRESET_SLUG)
        assert bases == []
