"""
test_marketplace_assistant_installer.py - installing an ``agent`` entity of
kind ``assistant``.

A helper package is an assistant folder: ``manifest.json`` with the profile
metadata, ``prompt.md`` with the system prompt and ``files/*.txt`` with the
attachments. These tests drive the real importer against a temporary
DATA_DIR, so they cover what is easy to get wrong: the profile reaches the
``assistants`` table, an attachment the user added survives an update, an
edited prompt is not overwritten, and a failure leaves neither a folder nor a
row behind.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    add_catalog_version,
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)


def _assistant_files(slug: str = "demo_helper", prompt: str = "You are a demo helper.\n") -> dict:
    """Return the file contents of a small but realistic helper package."""
    manifest = {
        "name": "Demo Helper",
        "description": "A helper used by the marketplace tests",
        "service": "DeepSeek",
        "model": "deepseek-flash",
        "temperature": 0.2,
        "tools": [],
        "max_tool_calls": 3,
    }
    return {
        "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2),
        "prompt.md": prompt,
        "files/notes.txt": "Reference note.\n",
    }


def _assistant_entity(
    tmp_path,
    serve_catalog,
    *,
    entity_id: str = "demo_helper",
    version: str = "1.0.0",
    files: dict = None,
    extra: dict = None,
):
    """Serve a catalog holding one helper and return (entity, base_url)."""
    payload = dict(extra or {})
    payload.setdefault("agent_kind", "assistant")
    entity, package = make_entity(
        entity_id=entity_id,
        version=version,
        type_="agent",
        files=files if files is not None else _assistant_files(entity_id),
        extra=payload,
    )
    root = make_catalog(tmp_path / "catalog", [(entity, package)])
    return entity, serve_catalog(root)


def _source(base_url: str):
    """Return a catalog source bound to *base_url*."""
    from core.marketplace.source import MarketplaceSource

    return MarketplaceSource(base_url)


def _add_version(tmp_path, entity: dict, *, version: str, files: dict):
    """Publish one more version of a helper into the same catalog.

    Rebuilding the manifest through ``make_entity`` keeps the per-file hashes
    and the package hash consistent with the archive that is actually served.
    """
    newer, package = make_entity(
        entity_id=str(entity["id"]),
        version=version,
        type_="agent",
        files=files,
        extra={"agent_kind": "assistant"},
    )
    add_catalog_version(tmp_path / "catalog", newer, package)
    return newer


class TestInstallAssistant:
    def test_creates_folder_row_and_registry_entry(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.assistant_folders import get_assistant_dir
        from core.assistants import get_assistant_by_slug
        from core.marketplace import registry
        from core.marketplace.agent_installer import install_agent

        entity, base_url = _assistant_entity(tmp_path, serve_catalog)
        report = install_agent(entity, channel="main", source=_source(base_url))

        assert report["ok"] is True
        assert report["agent_kind"] == "assistant"
        assert report["runtime_slug"] == "demo_helper"
        folder = get_assistant_dir("demo_helper")
        assert Path(folder, "manifest.json").is_file()
        assert Path(folder, "prompt.md").read_text(encoding="utf-8") == "You are a demo helper.\n"
        assert Path(folder, "files", "notes.txt").is_file()

        row = get_assistant_by_slug("demo_helper")
        assert row is not None
        assert row["name"] == "Demo Helper"
        assert row["service"] == "DeepSeek"

        record = registry.get_installed("agent", "demo_helper")
        assert record is not None
        assert record["agent_kind"] == "assistant"
        assert record["runtime_db_id"] == row["id"]
        assert "prompt.md" in record["files_json"]

    def test_reinstall_same_version_is_noop(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.agent_installer import install_agent

        entity, base_url = _assistant_entity(tmp_path, serve_catalog)
        first = install_agent(entity, channel="main", source=_source(base_url))
        second = install_agent(entity, channel="main", source=_source(base_url))

        assert second["noop"] is True
        assert second["install_id"] == first["install_id"]
        assert second["agent_kind"] == "assistant"

    def test_missing_prompt_is_refused(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.assistant_folders import assistant_folder_exists
        from core.marketplace.agent_installer import install_agent
        from core.marketplace.installer import InstallError

        files = _assistant_files()
        files["prompt.md"] = ""
        entity, base_url = _assistant_entity(tmp_path, serve_catalog, files=files)

        with pytest.raises(InstallError) as caught:
            install_agent(entity, channel="main", source=_source(base_url))
        assert caught.value.code == "missing_prompt"
        assert assistant_folder_exists("demo_helper") is False

    def test_binary_attachment_is_refused(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.agent_installer import install_agent
        from core.marketplace.installer import InstallError

        files = _assistant_files()
        files["files/scan.pdf"] = "not really a pdf\n"
        entity, base_url = _assistant_entity(tmp_path, serve_catalog, files=files)

        with pytest.raises(InstallError) as caught:
            install_agent(entity, channel="main", source=_source(base_url))
        assert caught.value.code == "unsupported_attachment"
        assert caught.value.details["files"] == ["files/scan.pdf"]

    def test_kind_change_is_refused(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace.agent_installer import install_agent
        from core.marketplace.installer import InstallError

        entity, base_url = _assistant_entity(tmp_path, serve_catalog)
        install_agent(entity, channel="main", source=_source(base_url))

        entity["agent_kind"] = "orchestrator"
        with pytest.raises(InstallError) as caught:
            install_agent(entity, channel="main", source=_source(base_url))
        assert caught.value.code == "agent_kind_changed"
        assert caught.value.details["installed"] == "assistant"

    def test_update_keeps_user_attachment_and_edited_prompt(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.assistant_folders import get_assistant_dir
        from core.assistants import save_assistant_prompt_text
        from core.marketplace.agent_installer import install_agent

        entity, base_url = _assistant_entity(tmp_path, serve_catalog)
        first = install_agent(entity, channel="main", source=_source(base_url))

        # The user adds a note of their own and edits the prompt.
        notes = Path(get_assistant_dir("demo_helper"), "files", "mine.txt")
        notes.write_text("my own note\n", encoding="utf-8")
        save_assistant_prompt_text(first["assistant_id"], "My own prompt.\n")

        newer = _add_version(
            tmp_path, entity, version="1.1.0",
            files=_assistant_files(prompt="The catalog prompt v1.1.\n"),
        )
        report = install_agent(newer, channel="main", source=_source(base_url))

        assert report["ok"] is True
        assert report["version"] == "1.1.0"
        assert "mine.txt" in report["preserved_attachments"]
        assert notes.read_text(encoding="utf-8") == "my own note\n"
        prompt = Path(get_assistant_dir("demo_helper"), "prompt.md")
        assert prompt.read_text(encoding="utf-8") == "My own prompt.\n"

    def test_update_replaces_untouched_prompt(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.assistant_folders import get_assistant_dir
        from core.marketplace.agent_installer import install_agent

        entity, base_url = _assistant_entity(tmp_path, serve_catalog)
        install_agent(entity, channel="main", source=_source(base_url))

        newer = _add_version(
            tmp_path, entity, version="1.1.0",
            files=_assistant_files(prompt="The catalog prompt v1.1.\n"),
        )
        install_agent(newer, channel="main", source=_source(base_url))

        prompt = Path(get_assistant_dir("demo_helper"), "prompt.md")
        assert prompt.read_text(encoding="utf-8") == "The catalog prompt v1.1.\n"

    def test_uninstall_removes_folder_row_and_writes_marker(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.assistant_folders import assistant_folder_exists
        from core.assistants import get_assistant_by_slug
        from core.marketplace import registry
        from core.marketplace.agent_installer import install_agent
        from core.marketplace.conflicts import uninstall

        entity, base_url = _assistant_entity(tmp_path, serve_catalog)
        install_agent(entity, channel="main", source=_source(base_url))

        report = uninstall("agent", "demo_helper")

        assert report["removed"] is True
        assert report["row_removed"] is True
        assert assistant_folder_exists("demo_helper") is False
        assert get_assistant_by_slug("demo_helper") is None
        assert registry.get_installed("agent", "demo_helper") is None
        assert registry.is_removed("agent", "demo_helper") is True


class TestAssistantInUpdates:
    def test_the_update_entry_states_the_kind(
        self, marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
    ):
        from core.marketplace import config as marketplace_config
        from core.marketplace import updates
        from core.marketplace.agent_installer import install_agent

        monkeypatch.setattr(
            marketplace_config,
            "marketplace_settings",
            lambda: {
                "enabled": True,
                "base_url": "",
                "allow_code": True,
                "require_signature": False,
                "channel": "main",
            },
        )
        entity, base_url = _assistant_entity(tmp_path, serve_catalog)
        install_agent(entity, channel="main", source=_source(base_url))

        newer = _add_version(
            tmp_path, entity, version="1.1.0",
            files=_assistant_files(prompt="The catalog prompt v1.1.\n"),
        )

        report = updates.check_entity_updates(source=_source(base_url))

        assert report["ok"] is True
        entry = next(item for item in report["agent"] if item["id"] == "demo_helper")
        assert entry["agent_kind"] == "assistant"
        assert entry["version"] == newer["version"]

    def test_a_skill_entry_carries_no_kind(
        self, marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
    ):
        from core.marketplace import config as marketplace_config
        from core.marketplace.source import MarketplaceSource
        from core.marketplace import updates
        from core.marketplace.installer import install_skill

        monkeypatch.setattr(
            marketplace_config,
            "marketplace_settings",
            lambda: {
                "enabled": True,
                "base_url": "",
                "allow_code": True,
                "require_signature": False,
                "channel": "main",
            },
        )
        skill, package = make_entity(version="1.0.0")
        root = make_catalog(tmp_path / "catalog", [(skill, package)])
        base_url = serve_catalog(root)
        install_skill(skill, channel="main", source=MarketplaceSource(base_url))

        newer, newer_package = make_entity(
            version="1.1.0", files={"SKILL.md": "# demo_skill v1.1\n"}
        )
        add_catalog_version(root, newer, newer_package)

        report = updates.check_entity_updates(source=_source(base_url))

        entry = report["skill"][0]
        assert entry["agent_kind"] == ""
