# -*- coding: utf-8 -*-
"""
tests.test_default_rag_bases - preset RAG bases and auto-assignment.

Covers:
  - core.defaults helpers (rag_bases_dir, list_default_rag_base_slugs,
    load_default_rag_base),
  - core.default_imports.ensure_default_rag_bases(): seeding from
    defaults/rag_bases, idempotency, and the removed-defaults protection
    recorded by core.rag.delete_base(),
  - preset orchestrator rag_bases: a fresh ya_agent gets the preset base
    assigned, an existing one gets it via union (backfill) without losing
    user settings, and the base appears in the built prompt metadata.
"""
import importlib
import json
import os
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


PRESET_SLUG = "ya_agent"
RAG_SLUG = "yaagentai_2020"


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR; defaults/rag_bases must contain a real preset."""
    import tempfile

    from tests._test_isolation import isolated_app_modules as _iso_app_modules

    tmp = tempfile.mkdtemp(prefix="sagaai_test_default_rag_")
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    with _iso_app_modules():
        import core.paths as paths_mod
        importlib.reload(paths_mod)

        if not os.path.isdir(
            os.path.join(paths_mod.DEFAULTS_DIR, "rag_bases", RAG_SLUG)
        ):
            pytest.skip(
                "defaults/rag_bases/yaagentai_2020 is not present in this checkout"
            )

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

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# ─── core.defaults helpers ────────────────────────────────────────────────────

class TestDefaultsRagHelpers:
    def test_helpers_see_bundled_base(self):
        from core import defaults

        slugs = defaults.list_default_rag_base_slugs()
        assert RAG_SLUG in slugs
        data = defaults.load_default_rag_base(RAG_SLUG)
        assert data is not None
        assert data.get("slug") == RAG_SLUG


# ─── default import of RAG bases ──────────────────────────────────────────────

class TestDefaultRagImport:
    def test_first_run_creates_runtime_base_with_source(self, isolated_data_dir):
        from core.default_imports import ensure_default_rag_bases
        from core import rag

        result = ensure_default_rag_bases()
        assert result.get(RAG_SLUG) == "created", result

        fresh = rag._load_manifest_raw(RAG_SLUG)
        assert fresh["slug"] == RAG_SLUG
        assert fresh["source"] == f"defaults/rag_bases/{RAG_SLUG}"
        assert fresh["status"] == "ready"

        import core.paths as paths_mod
        assert os.path.isfile(
            os.path.join(paths_mod.RAG_BASES_DIR, RAG_SLUG, "index.db")
        )
        stats = rag.get_base(RAG_SLUG).get("index_stats") or {}
        assert stats.get("chunks", 0) > 0

    def test_import_is_idempotent(self, isolated_data_dir):
        from core.default_imports import ensure_default_rag_bases

        assert ensure_default_rag_bases().get(RAG_SLUG) == "created"
        assert ensure_default_rag_bases().get(RAG_SLUG) == "exists"

    def test_existing_base_is_not_overwritten(self, isolated_data_dir):
        from core import rag
        from core.default_imports import ensure_default_rag_bases

        assert ensure_default_rag_bases().get(RAG_SLUG) == "created"
        assert rag.update_base(
            RAG_SLUG,
            {"name": "Моя пользовательская база", "description": "UPDATED"},
        )["name"] == "Моя пользовательская база"

        assert ensure_default_rag_bases().get(RAG_SLUG) == "exists"
        data = rag.get_base(RAG_SLUG)
        assert data["name"] == "Моя пользовательская база"
        assert data["description"] == "UPDATED"

    def test_deleted_default_base_is_not_resurrected(self, isolated_data_dir):
        from core import rag
        from core.default_imports import ensure_default_rag_bases

        assert ensure_default_rag_bases().get(RAG_SLUG) == "created"
        assert rag.delete_base(RAG_SLUG) is True
        assert rag.get_base(RAG_SLUG) == {}

        # The removed marker must prevent the next import from recreating it.
        assert ensure_default_rag_bases().get(RAG_SLUG) == "exists"
        assert rag.get_base(RAG_SLUG) == {}


# ─── preset orchestrator auto-assignment ──────────────────────────────────────

class TestPresetRagAssignment:
    def test_fresh_preset_gets_base_assigned(self, isolated_data_dir):
        from core import default_imports

        result = default_imports.ensure_all_defaults()
        assert result["orchestrators"].get(PRESET_SLUG) == "created", result

        from core.orchestrators import get_orchestrator

        cfg = get_orchestrator(PRESET_SLUG)["config"]
        assert cfg.get("rag_bases") == [RAG_SLUG]

    def test_backfill_existing_orchestrator_without_rag_bases(
        self, isolated_data_dir
    ):
        from core import default_imports

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
        assert cfg2["rag_bases"] == [RAG_SLUG]
        assert cfg2["strong_temperature"] == 0.123

    def test_explicit_user_assignment_is_preserved(self, isolated_data_dir):
        from core import default_imports

        default_imports.ensure_all_defaults()

        from core.orchestrators import get_orchestrator, save_orchestrator

        cfg = dict(get_orchestrator(PRESET_SLUG)["config"])
        cfg["rag_bases"] = ["custom_user_base"]
        save_orchestrator(PRESET_SLUG, config=cfg)

        default_imports.ensure_all_defaults()
        cfg2 = get_orchestrator(PRESET_SLUG)["config"]
        assert cfg2["rag_bases"] == ["custom_user_base"]

    def test_flags_listed_in_metadata_block(self, isolated_data_dir):
        from core import default_imports

        result = default_imports.ensure_all_defaults()
        assert result["orchestrators"].get(PRESET_SLUG) == "created", result

        from core.orchestrators import build_assistant_dicts

        strong, _weak = build_assistant_dicts(PRESET_SLUG)
        prompt = strong.get("text") or ""
        assert "Available RAG knowledge bases" in prompt
        assert RAG_SLUG in prompt

    def test_flag_can_be_removed_by_user(self, isolated_data_dir):
        from core import default_imports

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
        assert get_orchestrator_rag_bases(PRESET_SLUG) == []
