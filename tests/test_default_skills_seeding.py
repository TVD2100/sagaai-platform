# -*- coding: utf-8 -*-
"""
tests.test_default_skills_seeding - the bundled skills a fresh install gets.

The Rag Base Creator skill ships inside the platform and must be usable right
away. The catalog offers the same skill, and a catalog installation and the
bundled copy share one folder name - so the seeding rule and the catalog have
to agree that one folder means one skill.

Covered here:

  * the bundled Rag Base Creator is seeded on a fresh installation, with the
    marketplace switched on (the case that used to suppress it);
  * a skill the catalog already installed is never seeded a second time -
    neither under a suffixed folder nor as a duplicate record;
  * a seeded bundled skill is adopted by the catalog registry, so the
    showcase reports it as installed instead of offering a second copy.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import marketplace_data_dir  # noqa: E402,F401 - fixture

BUNDLED_SKILL = "rag_base_creator"


@pytest.fixture
def preset_root(tmp_path, monkeypatch):
    """Point ``defaults/skills`` at a folder holding one bundled preset."""
    import core.defaults as defaults_mod

    root = tmp_path / "defaults_skills"
    preset = root / BUNDLED_SKILL
    preset.mkdir(parents=True)
    (preset / "SKILL.md").write_text("# Rag Base Creator\n", encoding="utf-8")
    monkeypatch.setattr(defaults_mod, "skills_dir", lambda: str(root))
    return root


def _records_for(folder: str):
    """Return every library record whose folder is *folder*."""
    from core import skills_library

    return [
        record
        for record in skills_library._load_registry().values()
        if isinstance(record, dict) and str(record.get("folder") or "") == folder
    ]


def _install_as_catalog_skill(folder: str = BUNDLED_SKILL) -> str:
    """Create a skill record that looks like a catalog installation."""
    from core import skills_library

    root = skills_library.ensure_skills_root()
    source = os.path.join(root, folder)
    os.makedirs(source, exist_ok=True)
    with open(os.path.join(source, "SKILL.md"), "w", encoding="utf-8") as handle:
        handle.write("# Installed from the catalog\n")
    skill_id = skills_library.register_skill("Rag Base Creator", "", folder)
    registry_data = skills_library._load_registry()
    registry_data[skill_id]["source"] = f"marketplace/{folder}"
    skills_library._save_registry(registry_data)
    return skill_id


class TestBundledSkillIsSeeded:
    def test_rag_base_creator_moved_out_of_the_catalog_only_list(self):
        """The skill ships with the platform, so it must not be catalog-only."""
        from core import default_imports

        assert BUNDLED_SKILL not in default_imports.CATALOG_ONLY_SKILLS

    def test_bundled_skill_is_seeded_with_the_marketplace_on(
        self, marketplace_data_dir, preset_root
    ):
        """A fresh installation gets the skill without opening the catalog."""
        from core import default_imports, skills_library

        assert default_imports._marketplace_active() is True

        results = default_imports.ensure_default_skills()

        skill_id = results.get(BUNDLED_SKILL)
        assert skill_id and skill_id != "exists"
        record = skills_library.get_skill(skill_id)
        assert record is not None
        assert record["folder"] == BUNDLED_SKILL
        assert skills_library._load_registry()[skill_id]["source"] == f"defaults/{BUNDLED_SKILL}"

    def test_seeding_is_idempotent(self, marketplace_data_dir, preset_root):
        from core import default_imports, skills_library

        default_imports.ensure_default_skills()
        second = default_imports.ensure_default_skills()

        assert second.get(BUNDLED_SKILL) == "exists"
        assert len(_records_for(BUNDLED_SKILL)) == 1
        assert len(skills_library.list_skills()) == 1


class TestNoDuplicateWithACatalogInstall:
    def test_catalog_installation_owns_the_folder(
        self, marketplace_data_dir, preset_root
    ):
        """The bundled preset must not land next to a catalog copy."""
        from core import default_imports

        skill_id = _install_as_catalog_skill()

        results = default_imports.ensure_default_skills()

        assert results.get(BUNDLED_SKILL) == "exists"
        records = _records_for(BUNDLED_SKILL)
        assert [record.get("source") for record in records] == [f"marketplace/{BUNDLED_SKILL}"]
        from core import skills_library

        assert skills_library.get_skill(skill_id) is not None
        assert not os.path.isdir(os.path.join(skills_library.get_skills_root(), f"{BUNDLED_SKILL}_2"))

    def test_catalog_source_marker_suppresses_the_seed(
        self, marketplace_data_dir, preset_root
    ):
        """A record that only carries the catalog marker still wins.

        The folder may have been renamed by the installer; the marker alone
        is the statement "this skill came from the catalog".
        """
        from core import default_imports, skills_library

        root = skills_library.ensure_skills_root()
        source = os.path.join(root, "installed_elsewhere")
        os.makedirs(source, exist_ok=True)
        with open(os.path.join(source, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("# Installed from the catalog\n")
        skill_id = skills_library.register_skill("Rag Base Creator", "", "installed_elsewhere")
        registry_data = skills_library._load_registry()
        registry_data[skill_id]["source"] = f"marketplace/{BUNDLED_SKILL}"
        skills_library._save_registry(registry_data)

        results = default_imports.ensure_default_skills()

        assert results.get(BUNDLED_SKILL) == "exists"
        assert _records_for(BUNDLED_SKILL) == []
        assert len(skills_library.list_skills()) == 1


class TestCatalogAdoptsTheSeededSkill:
    def test_seeded_skill_becomes_an_installed_catalog_entity(
        self, marketplace_data_dir, preset_root
    ):
        """The catalog card reports the bundled copy as installed."""
        from core import default_imports
        from core.marketplace import migration, registry

        registry.ensure_tables()
        default_imports.ensure_default_skills()

        migration.seed_legacy_entities()

        row = registry.get_installed("skill", BUNDLED_SKILL)
        assert row is not None
        assert row["runtime_slug"] == BUNDLED_SKILL
        assert row["channel"] == "legacy"
