"""
test_marketplace_agent_installer.py - installing an ``agent`` entity.

An agent package is an orchestrator folder. These tests drive the real
importer against a temporary DATA_DIR, so they cover the parts that are easy
to get wrong: the config merge happens before the import, the instruction and
function files reach the folder, a taken slug is suffixed instead of
overwriting an existing employee, and a failure leaves neither a folder nor a
database row behind.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)


def _agent_files(slug: str = "demo_agent") -> dict:
    """Return the file contents of a small but realistic agent package."""
    bundle = {
        "format": "sagaai_orchestrator/v1",
        "slug": slug,
        "name": "Demo Agent",
        "description": "An agent used by the marketplace tests",
        "config": {"strong_service": "YandexAI", "strong_model": "from_package"},
        "tools": [],
        "max_steps": 42,
    }
    instruction = (
        "---\nid: rules\nname: Rules\ndescription: How this agent behaves\n---\n\n"
        "Always answer briefly.\n"
    )
    function = "def invoke(**kwargs):\n    return {'ok': True}\n"
    return {
        "orchestrator.json": json.dumps(bundle, ensure_ascii=False, indent=2),
        "system_prompt.md": "You are a demo agent.\n",
        "instructions/rules.md": instruction,
        "functions/ping.py": function,
    }


def _agent_entity(tmp_path, serve_catalog, *, entity_id="demo_agent", version="1.0.0",
                  extra=None, files=None):
    """Serve a catalog holding one agent and return (entity, base_url)."""
    entity, package = make_entity(
        entity_id=entity_id,
        version=version,
        type_="agent",
        files=files if files is not None else _agent_files(entity_id),
        extra=extra,
    )
    entity.setdefault("agent_kind", "orchestrator")
    root = make_catalog(tmp_path / "catalog", [(entity, package)])
    return entity, serve_catalog(root)


def _source(base_url: str):
    """Return a catalog source bound to *base_url*."""
    from core.marketplace.source import MarketplaceSource

    return MarketplaceSource(base_url)


class TestInstallAgent:
    def test_creates_orchestrator_folder_and_row(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import registry
        from core.marketplace.agent_installer import install_agent
        from core.orchestrator_folders import get_orchestrator_dir
        from core.orchestrators import get_orchestrator

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        report = install_agent(entity, channel="main", source=_source(base_url))

        assert report["ok"] is True
        assert report["runtime_slug"] == "demo_agent"
        folder = get_orchestrator_dir("demo_agent")
        assert Path(folder, "system_prompt.md").is_file()
        assert Path(folder, "instructions", "rules.md").is_file()
        assert Path(folder, "functions", "ping.py").is_file()

        orch = get_orchestrator("demo_agent")
        assert orch is not None
        assert orch["prompt_text"] == "You are a demo agent.\n"

        row = registry.get_installed("agent", "demo_agent")
        assert row is not None
        assert row["version"] == "1.0.0"
        assert row["runtime_db_id"] == orch["id"]

    def test_imports_instructions_and_functions(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.agent_installer import install_agent
        from core.orchestrators import (
            get_orchestrator_function,
            get_orchestrator_instruction,
        )

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        report = install_agent(entity, channel="main", source=_source(base_url))

        assert report["instructions_imported"] == 1
        assert report["functions_imported"] == 1
        instruction = get_orchestrator_instruction("demo_agent", "rules")
        assert instruction["name"] == "Rules"
        assert "Always answer briefly" in instruction["text"]
        function = get_orchestrator_function("demo_agent", "ping")
        assert "def invoke" in function["code"]

    def test_config_defaults_fill_gaps_but_never_overwrite(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.agent_installer import install_agent
        from core.orchestrators import get_orchestrator

        entity, base_url = _agent_entity(
            tmp_path,
            serve_catalog,
            extra={"config_defaults": {
                "strong_model": "from_manifest",
                "reasoning_effort": "high",
            }},
        )
        install_agent(entity, channel="main", source=_source(base_url))

        config = get_orchestrator("demo_agent")["config"]
        # The package ships a tested model; the manifest only fills gaps.
        assert config["strong_model"] == "from_package"
        assert config["reasoning_effort"] == "high"
        assert config["marketplace"]["id"] == "demo_agent"
        assert config["marketplace"]["channel"] == "main"

    def test_second_install_of_same_version_is_a_noop(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.agent_installer import install_agent

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        first = install_agent(entity, channel="main", source=_source(base_url))
        second = install_agent(entity, channel="main", source=_source(base_url))

        assert first["noop"] is False
        assert second["noop"] is True
        assert second["runtime_slug"] == first["runtime_slug"]

    def test_taken_slug_gets_a_suffix(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.agent_installer import install_agent
        from core.orchestrators import create_orchestrator, get_orchestrator

        create_orchestrator(
            slug="demo_agent",
            name="Someone else",
            prompt_text="Existing employee.\n",
        )
        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        report = install_agent(entity, channel="main", source=_source(base_url))

        assert report["runtime_slug"] == "demo_agent_2"
        # The pre-existing employee is untouched.
        assert get_orchestrator("demo_agent")["prompt_text"] == "Existing employee.\n"
        assert get_orchestrator("demo_agent_2") is not None

    def test_strict_mode_skips_functions(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.agent_installer import install_agent
        from core.orchestrator_folders import get_orchestrator_dir

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        report = install_agent(
            entity, channel="main", source=_source(base_url), allow_code=False
        )

        assert report["skipped_code_files"] == ["functions/ping.py"]
        assert not Path(get_orchestrator_dir("demo_agent"), "functions", "ping.py").is_file()
        assert Path(get_orchestrator_dir("demo_agent"), "system_prompt.md").is_file()

    def test_live_folder_manifest_is_clean_after_install(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import registry, updates
        from core.marketplace.agent_installer import install_agent

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        install_agent(entity, channel="main", source=_source(base_url))

        row = registry.get_installed("agent", "demo_agent")
        assert row["files_json"], "the registry row must carry the file manifest"
        # The importer rewrites the bundle and the prompt, so a fresh install
        # reporting itself as modified would be the classic hash-scope bug.
        state = updates.verify_installed("agent")
        assert state["modified"] == []
        assert row["tree_sha256"] == registry.manifest_sha256(row["files_json"])

    def test_user_notes_are_not_part_of_the_entity(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import updates
        from core.marketplace.agent_installer import install_agent
        from core.orchestrator_folders import get_orchestrator_dir

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        install_agent(entity, channel="main", source=_source(base_url))
        Path(get_orchestrator_dir("demo_agent"), "my_notes.md").write_text(
            "scratch notes\n", encoding="utf-8"
        )

        state = updates.verify_installed("agent")
        assert state["modified"] == []


class TestUpdateAgent:
    """Updating an installed agent refreshes it in place (D-06, D-08)."""

    def test_update_keeps_the_folder_and_does_not_duplicate(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import registry
        from core.marketplace.agent_installer import install_agent
        from core.orchestrator_folders import get_orchestrator_dir
        from core.orchestrators import get_orchestrator
        from tests.marketplace_fixtures import add_catalog_version, make_entity

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        first = install_agent(entity, channel="main", source=_source(base_url))

        newer, package = make_entity(
            entity_id="demo_agent",
            version="1.1.0",
            type_="agent",
            files=_agent_files("demo_agent"),
        )
        newer.setdefault("agent_kind", "orchestrator")
        root = tmp_path / "catalog"
        add_catalog_version(root, newer, package)

        second = install_agent(newer, channel="main", source=_source(base_url))

        assert second["ok"] is True
        assert second["noop"] is False
        # An update lands in the folder the agent already occupies: a second
        # copy next to it would be a different employee with a copy of its
        # settings, which is exactly what an update must not create.
        assert second["runtime_slug"] == first["runtime_slug"] == "demo_agent"
        assert get_orchestrator_dir("demo_agent_2") is None or not Path(
            get_orchestrator_dir("demo_agent_2")
        ).is_dir()
        assert get_orchestrator("demo_agent") is not None
        assert registry.get_installed("agent", "demo_agent")["version"] == "1.1.0"

    def test_update_keeps_settings_the_user_changed(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.agent_installer import install_agent
        from core.orchestrators import get_orchestrator, save_orchestrator
        from tests.marketplace_fixtures import add_catalog_version, make_entity

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        install_agent(entity, channel="main", source=_source(base_url))
        # The user picks a model of their own; the update ships a different one.
        config = dict(get_orchestrator("demo_agent")["config"])
        config["strong_model"] = "my_own_model"
        save_orchestrator("demo_agent", config=config)

        files = _agent_files("demo_agent")
        bundle = json.loads(files["orchestrator.json"])
        bundle["config"]["strong_model"] = "newer_catalog_model"
        files["orchestrator.json"] = json.dumps(bundle, ensure_ascii=False, indent=2)
        newer, package = make_entity(
            entity_id="demo_agent", version="1.1.0", type_="agent", files=files
        )
        newer.setdefault("agent_kind", "orchestrator")
        add_catalog_version(tmp_path / "catalog", newer, package)

        install_agent(newer, channel="main", source=_source(base_url))

        updated = get_orchestrator("demo_agent")["config"]
        assert updated["strong_model"] == "my_own_model"
        # A key the user never touched follows the package.
        assert updated["marketplace"]["version"] == "1.1.0"

    def test_update_adopts_a_package_value_the_user_left_alone(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.agent_installer import install_agent
        from core.orchestrators import get_orchestrator
        from tests.marketplace_fixtures import add_catalog_version, make_entity

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        install_agent(entity, channel="main", source=_source(base_url))
        assert get_orchestrator("demo_agent")["config"]["strong_model"] == "from_package"

        files = _agent_files("demo_agent")
        bundle = json.loads(files["orchestrator.json"])
        bundle["config"]["strong_model"] = "newer_catalog_model"
        files["orchestrator.json"] = json.dumps(bundle, ensure_ascii=False, indent=2)
        newer, package = make_entity(
            entity_id="demo_agent", version="1.1.0", type_="agent", files=files
        )
        newer.setdefault("agent_kind", "orchestrator")
        add_catalog_version(tmp_path / "catalog", newer, package)

        install_agent(newer, channel="main", source=_source(base_url))

        assert get_orchestrator("demo_agent")["config"]["strong_model"] == "newer_catalog_model"

    def test_update_drops_files_the_new_version_no_longer_ships(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.agent_installer import install_agent
        from core.orchestrator_folders import get_orchestrator_dir
        from tests.marketplace_fixtures import add_catalog_version, make_entity

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        install_agent(entity, channel="main", source=_source(base_url))
        folder = Path(get_orchestrator_dir("demo_agent"))
        assert (folder / "instructions" / "rules.md").is_file()

        files = _agent_files("demo_agent")
        files.pop("instructions/rules.md")
        files["instructions/other.md"] = (
            "---\nid: other\nname: Other\ndescription: Replacement\n---\n\nBe terse.\n"
        )
        newer, package = make_entity(
            entity_id="demo_agent", version="1.1.0", type_="agent", files=files
        )
        newer.setdefault("agent_kind", "orchestrator")
        add_catalog_version(tmp_path / "catalog", newer, package)

        install_agent(newer, channel="main", source=_source(base_url))

        assert not (folder / "instructions" / "rules.md").is_file()
        assert (folder / "instructions" / "other.md").is_file()

    def test_failed_update_restores_the_previous_files(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        """A crash mid-update restores the version that was there."""
        from core.marketplace import rollback, store
        from core.marketplace.agent_installer import install_agent
        from core.orchestrator_folders import get_orchestrator_dir
        from tests.marketplace_fixtures import add_catalog_version, make_entity

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        first = install_agent(entity, channel="main", source=_source(base_url))
        folder = Path(get_orchestrator_dir("demo_agent"))
        before = (folder / "system_prompt.md").read_text(encoding="utf-8")
        assert first["noop"] is False

        files = _agent_files("demo_agent")
        files["system_prompt.md"] = "A brand new prompt.\n"
        newer, package = make_entity(
            entity_id="demo_agent", version="1.1.0", type_="agent", files=files
        )
        newer.setdefault("agent_kind", "orchestrator")
        add_catalog_version(tmp_path / "catalog", newer, package)
        update = install_agent(newer, channel="main", source=_source(base_url))
        assert (folder / "system_prompt.md").read_text(encoding="utf-8") != before

        # Rolling back the update restores the version that was replaced; the
        # run of the first install owns the folder itself and is not the one
        # being undone here.
        report = rollback.rollback_run(update["install_id"])

        assert report["restored_files"], report
        assert (folder / "system_prompt.md").read_text(encoding="utf-8") == before
        assert Path(folder).is_dir()
        assert get_orchestrator_dir("demo_agent") is not None


class TestInstallAgentFailures:
    def test_package_without_prompt_is_refused(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.agent_installer import install_agent
        from core.marketplace.installer import InstallError
        from core.orchestrator_folders import orchestrator_folder_exists

        files = _agent_files()
        files.pop("system_prompt.md")
        entity, base_url = _agent_entity(tmp_path, serve_catalog, files=files)

        with pytest.raises(InstallError) as caught:
            install_agent(entity, channel="main", source=_source(base_url))
        assert caught.value.code == "missing_prompt"
        assert orchestrator_folder_exists("demo_agent") is False

    def test_unsupported_agent_kind_is_refused(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.agent_installer import install_agent
        from core.marketplace.installer import InstallError

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        entity["agent_kind"] = "widget"

        with pytest.raises(InstallError) as caught:
            install_agent(entity, channel="main", source=_source(base_url))
        assert caught.value.code == "unsupported_agent_kind"
        assert caught.value.details["agent_kind"] == "widget"

    def test_hash_mismatch_leaves_nothing_behind(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import registry
        from core.marketplace.agent_installer import install_agent
        from core.marketplace.installer import InstallError
        from core.orchestrator_folders import orchestrator_folder_exists

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        entity["files"]["system_prompt.md"] = "0" * 64

        with pytest.raises(InstallError) as caught:
            install_agent(entity, channel="main", source=_source(base_url))
        assert caught.value.code == "file_hash_mismatch"
        assert orchestrator_folder_exists("demo_agent") is False
        assert registry.get_installed("agent", "demo_agent") is None


class TestDispatcher:
    def test_dispatch_installs_an_agent(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import registry
        from core.marketplace.installer import install_entity

        entity, base_url = _agent_entity(tmp_path, serve_catalog)
        report = install_entity(entity, channel="main", source=_source(base_url))

        assert report["ok"] is True
        assert registry.get_installed("agent", "demo_agent") is not None

    def test_dispatch_refuses_unhandled_types(
        self, marketplace_data_dir
    ):
        """Every type the catalog can carry has an installer; anything else
        is refused by the dispatcher instead of reaching a wrong branch."""
        from core.marketplace.installer import InstallError, install_entity

        with pytest.raises(InstallError) as caught:
            install_entity({"id": "x", "type": "widget"}, channel="main")
        assert caught.value.code == "unsupported_type"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
