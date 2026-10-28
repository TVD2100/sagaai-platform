"""
test_marketplace_publish.py - building the catalog repository.

The publisher is what turns an entity folder into the files the catalog
serves, so the tests pin the three properties the platform relies on: a
package hashes to the same bytes on every rebuild, a published version never
changes under a pinned client, and the index it writes is readable by the
transport and verifiable by its signature.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.marketplace import publish as publish_mod  # noqa: E402
from core.marketplace import catalog, source as source_mod  # noqa: E402
from tests.marketplace_fixtures import (  # noqa: E402
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,  # noqa: F401 - fixture
)


SKILL_FILES = {
    "SKILL.md": "# Demo\n\nInstructions.\n",
    "scripts/run.py": "print('demo')\n",
}


AGENT_FILES = {
    "bundle.json": "{\"description\": \"Demo employee\"}\n",
    "system_prompt.md": "## Role\nYou help.\n",
}


def _write_folder(root: Path, files: dict) -> Path:
    """Write ``{relative path: text}`` under *root* and return it."""
    for rel_path, text in files.items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def _manifest(entity_id="demo_skill", version="1.0.0", type_="skill", **extra):
    """Return a descriptive manifest (no hashes - the publisher fills them)."""
    manifest = {
        "id": entity_id,
        "type": type_,
        "version": version,
        "name": {"en": "Demo", "ru": "Демо", "zh-CN": "演示"},
        "description": {"en": "Demo entity", "ru": "Демо-сущность", "zh-CN": "演示实体"},
        "license": {"spdx": "PolyForm-Noncommercial-1.0.0", "acceptance_required": False},
        "permissions": {"executes_code": True},
        "platform": {"min": "1.0.0"},
    }
    manifest.update(extra)
    return manifest


# ─── Deterministic package ────────────────────────────────────────────────

class TestPackage:
    def test_the_same_files_produce_the_same_bytes(self):
        """A rebuild must not change the hash - the cache keys on it."""
        files = {
            "b.md": b"second\n",
            "a.md": b"first\n",
            "dir/c.py": b"print(1)\n",
        }
        first = publish_mod.build_package(files)
        second = publish_mod.build_package(dict(reversed(list(files.items()))))

        assert first == second
        assert publish_mod.sha256_bytes(first) == publish_mod.sha256_bytes(second)

    def test_a_changed_file_changes_the_package(self):
        one = publish_mod.build_package({"a.md": b"one\n"})
        two = publish_mod.build_package({"a.md": b"two\n"})

        assert one != two

    def test_the_package_unpacks_with_the_declared_names(self, tmp_path):
        import tarfile

        package = publish_mod.build_package({"a.md": b"x", "d/b.md": b"y"})
        archive_path = tmp_path / "p.tar.gz"
        archive_path.write_bytes(package)

        with tarfile.open(archive_path, "r:gz") as archive:
            names = sorted(member.name for member in archive.getmembers())

        assert names == ["a.md", "d/b.md"]

    def test_hidden_and_bytecode_files_are_not_collected(self, tmp_path):
        _write_folder(tmp_path, SKILL_FILES)
        (tmp_path / ".DS_Store").write_bytes(b"junk")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"junk")

        files = publish_mod.collect_files(str(tmp_path))

        assert sorted(files) == ["SKILL.md", "scripts/run.py"]

    def test_an_empty_folder_is_refused(self, tmp_path):
        with pytest.raises(publish_mod.PublishError) as excinfo:
            publish_mod.collect_files(str(tmp_path))

        assert excinfo.value.code == "empty_folder"


# ─── Publish ──────────────────────────────────────────────────────────────

class TestPublishEntity:
    def test_it_writes_the_version_folder_and_the_latest_pointer(self, tmp_path):
        folder = _write_folder(tmp_path / "src", SKILL_FILES)
        root = tmp_path / "catalog"

        report = publish_mod.publish_entity(
            str(root), str(folder), _manifest(),
        )

        assert report["changed"] is True
        assert report["path"] == "skills/demo_skill/1.0.0/entity.json"
        written = json.loads((root / report["path"]).read_text(encoding="utf-8"))
        assert written["files"]["SKILL.md"] == publish_mod.sha256_bytes(b"# Demo\n\nInstructions.\n")
        assert written["package"]["url"] == (
            "skills/demo_skill/1.0.0/package.tar.gz"
        )
        latest = root / "skills/demo_skill/latest/entity.json"
        assert json.loads(latest.read_text(encoding="utf-8"))["version"] == "1.0.0"
        assert (root / "skills/demo_skill/1.0.0/package.tar.gz").is_file()

    def test_publishing_the_same_content_twice_is_a_noop(self, tmp_path):
        folder = _write_folder(tmp_path / "src", SKILL_FILES)
        root = tmp_path / "catalog"
        first = publish_mod.publish_entity(str(root), str(folder), _manifest())

        second = publish_mod.publish_entity(str(root), str(folder), _manifest())

        assert first["package_sha256"] == second["package_sha256"]
        assert second["changed"] is False

    def test_the_same_version_with_other_content_is_refused(self, tmp_path):
        """A pinned client must never receive different bytes for a version."""
        root = tmp_path / "catalog"
        first = _write_folder(tmp_path / "one", SKILL_FILES)
        publish_mod.publish_entity(str(root), str(first), _manifest())
        second = _write_folder(tmp_path / "two", {"SKILL.md": "# Other\n"})

        with pytest.raises(publish_mod.PublishError) as excinfo:
            publish_mod.publish_entity(str(root), str(second), _manifest())

        assert excinfo.value.code == "version_exists"

    def test_the_latest_pointer_follows_the_newest_version(self, tmp_path):
        root = tmp_path / "catalog"
        folder = _write_folder(tmp_path / "src", SKILL_FILES)
        publish_mod.publish_entity(str(root), str(folder), _manifest(version="1.0.0"))

        publish_mod.publish_entity(str(root), str(folder), _manifest(version="2.0.0"))
        publish_mod.publish_entity(str(root), str(folder), _manifest(version="1.5.0"))

        latest = json.loads(
            (root / "skills/demo_skill/latest/entity.json").read_text(encoding="utf-8")
        )
        assert latest["version"] == "2.0.0"
        assert publish_mod.list_versions(str(root), "skill", "demo_skill") == [
            "2.0.0", "1.5.0", "1.0.0",
        ]

    def test_a_pre_release_sorts_below_its_release(self, tmp_path):
        root = tmp_path / "catalog"
        folder = _write_folder(tmp_path / "src", SKILL_FILES)
        publish_mod.publish_entity(str(root), str(folder), _manifest(version="2.0.0-rc.1"))
        publish_mod.publish_entity(str(root), str(folder), _manifest(version="2.0.0"))

        assert publish_mod.list_versions(str(root), "skill", "demo_skill")[0] == "2.0.0"

    def test_an_invalid_id_or_version_is_refused(self, tmp_path):
        folder = _write_folder(tmp_path / "src", SKILL_FILES)
        root = tmp_path / "catalog"

        with pytest.raises(publish_mod.PublishError) as bad_id:
            publish_mod.publish_entity(
                str(root), str(folder), _manifest(entity_id="Bad/Id")
            )
        assert bad_id.value.code == "invalid_id"

        with pytest.raises(publish_mod.PublishError) as bad_version:
            publish_mod.publish_entity(
                str(root), str(folder), _manifest(version="1.0")
            )
        assert bad_version.value.code == "invalid_version"

    def test_an_unknown_type_is_refused(self, tmp_path):
        folder = _write_folder(tmp_path / "src", SKILL_FILES)

        with pytest.raises(publish_mod.PublishError) as excinfo:
            publish_mod.publish_entity(
                str(tmp_path / "catalog"), str(folder),
                _manifest(type_="widget"),
            )

        assert excinfo.value.code == "invalid_type"

    def test_an_agent_with_an_unknown_kind_is_refused(self, tmp_path):
        folder = _write_folder(tmp_path / "src", AGENT_FILES)

        with pytest.raises(publish_mod.PublishError) as excinfo:
            publish_mod.publish_entity(
                str(tmp_path / "catalog"), str(folder),
                _manifest(entity_id="demo_agent", type_="agent", agent_kind="robot"),
            )

        assert excinfo.value.code == "invalid_agent_kind"
        assert excinfo.value.details["agent_kind"] == "robot"

    def test_an_agent_without_a_kind_publishes_as_an_employee(self, tmp_path):
        """A manifest written before the kind existed describes an employee."""
        root = tmp_path / "catalog"
        folder = _write_folder(tmp_path / "src", AGENT_FILES)

        report = publish_mod.publish_entity(
            str(root), str(folder), _manifest(entity_id="demo_agent", type_="agent")
        )

        assert report["changed"] is True
        written = json.loads((root / report["path"]).read_text(encoding="utf-8"))
        assert written["agent_kind"] == "orchestrator"

    def test_an_entity_without_files_publishes_from_its_manifest(self, tmp_path):
        """An MCP entity is a preset: no package, and the manifest is the content."""
        root = tmp_path / "catalog"
        manifest = _manifest(entity_id="demo_mcp", type_="mcp", preset={"config": {"transport": "http"}})

        report = publish_mod.publish_entity(str(root), None, manifest)

        assert report["changed"] is True
        assert report["file_count"] == 0
        assert report["package_sha256"] == ""
        written = json.loads((root / report["path"]).read_text(encoding="utf-8"))
        assert "files" not in written
        assert "package" not in written
        assert written["preset"]["config"]["transport"] == "http"
        assert not (root / "mcp/demo_mcp/1.0.0/package.tar.gz").exists()

    def test_a_file_backed_type_cannot_skip_its_package(self, tmp_path):
        with pytest.raises(publish_mod.PublishError) as excinfo:
            publish_mod.publish_entity(str(tmp_path / "catalog"), None, _manifest())

        assert excinfo.value.code == "folder_required"

    def test_a_file_less_entity_notices_a_manifest_change(self, tmp_path):
        """With no package to compare, the manifest itself decides `changed`."""
        root = tmp_path / "catalog"
        manifest = _manifest(entity_id="demo_mcp", type_="mcp", preset={"config": {}})
        publish_mod.publish_entity(str(root), None, manifest)

        again = publish_mod.publish_entity(str(root), None, manifest)
        assert again["changed"] is False

        with pytest.raises(publish_mod.PublishError) as excinfo:
            publish_mod.publish_entity(
                str(root), None, dict(manifest, description={"en": "Other text"})
            )
        assert excinfo.value.code == "version_exists"


# ─── Index ────────────────────────────────────────────────────────────────

class TestIndex:
    def test_the_index_lists_every_entity_of_every_type(self, tmp_path):
        root = tmp_path / "catalog"
        skill = _write_folder(tmp_path / "skill", SKILL_FILES)
        server = _write_folder(tmp_path / "mcp", {"README.md": "server\n"})
        publish_mod.publish_entity(str(root), str(skill), _manifest())
        publish_mod.publish_entity(
            str(root), str(server),
            _manifest(entity_id="demo_mcp", type_="mcp", preset={"name": "Demo"}),
        )

        index = publish_mod.rebuild_index(str(root))

        types = sorted(entry["type"] for entry in index["entities"])
        assert types == ["mcp", "skill"]
        assert index["schema"] == publish_mod.INDEX_SCHEMA
        written = json.loads((root / "index.json").read_text(encoding="utf-8"))
        assert written == index

    def test_the_platform_reads_the_published_catalog(
        self, serve_catalog, tmp_path
    ):
        """The index the publisher writes is the one the showcase consumes."""
        root = tmp_path / "catalog"
        folder = _write_folder(tmp_path / "src", SKILL_FILES)
        publish_mod.publish_entity(str(root), str(folder), _manifest())
        publish_mod.rebuild_index(str(root))
        base_url = serve_catalog(root)

        listing = catalog.list_entities(
            "all", source=source_mod.MarketplaceSource(base_url)
        )
        assert listing["ok"] is True
        assert [item["id"] for item in listing["items"]] == ["demo_skill"]

        card = catalog.fetch_card(
            "demo_skill", source=source_mod.MarketplaceSource(base_url), type_="skill"
        )
        assert card["ok"] is True
        assert card["version"] == "1.0.0"

    def test_the_published_package_verifies_and_installs(
        self, marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
    ):
        """End to end: publish, serve, verify the hash, install the files."""
        from core.marketplace import config as marketplace_config
        from core.marketplace import installer, registry, verify

        monkeypatch.setattr(
            marketplace_config, "marketplace_settings",
            lambda: {"enabled": True, "base_url": "", "allow_code": True,
                     "require_signature": False, "channel": "main"},
        )
        root = tmp_path / "catalog"
        folder = _write_folder(tmp_path / "src", SKILL_FILES)
        publish_mod.publish_entity(str(root), str(folder), _manifest())
        publish_mod.rebuild_index(str(root))
        base_url = serve_catalog(root)
        transport = source_mod.MarketplaceSource(base_url)

        entity = transport.fetch_entity("skill", "demo_skill")
        package = transport.fetch_package(entity)
        digest = verify.verify_package(package, entity["package"]["sha256"])
        assert digest == entity["package"]["sha256"]

        report = installer.install_entity(
            entity, channel="main", source=transport
        )
        assert report["ok"] is True
        row = registry.get_installed("skill", "demo_skill")
        assert row["version"] == "1.0.0"
        installed = Path(registry.skills_root()) / row["runtime_slug"] / "SKILL.md"
        assert installed.is_file()


# ─── Signature ────────────────────────────────────────────────────────────

class TestSignature:
    def test_a_signed_index_verifies(self, tmp_path):
        root = tmp_path / "catalog"
        keys = tmp_path / "private"
        key_id = "test-key"
        publish_mod.generate_keypair(
            str(keys / "marketplace.pem"),
            str(root / "keys" / f"{key_id}.pub"),
            key_id,
        )
        folder = _write_folder(tmp_path / "src", SKILL_FILES)

        report = publish_mod.publish(
            str(root), str(folder), _manifest(),
            private_key_path=str(keys / "marketplace.pem"), key_id=key_id,
        )

        assert report["signed"] is True
        check = publish_mod.verify_index_signature(
            str(root), str(root / "keys" / f"{key_id}.pub")
        )
        assert check["ok"] is True
        assert check["key_id"] == key_id

    def test_an_edited_index_stops_verifying(self, tmp_path):
        """The signature covers the whole index, not just its entities."""
        root = tmp_path / "catalog"
        key_id = "test-key"
        publish_mod.generate_keypair(
            str(tmp_path / "private" / "m.pem"),
            str(root / "keys" / f"{key_id}.pub"),
            key_id,
        )
        folder = _write_folder(tmp_path / "src", SKILL_FILES)
        publish_mod.publish(
            str(root), str(folder), _manifest(),
            private_key_path=str(tmp_path / "private" / "m.pem"), key_id=key_id,
        )
        index_path = root / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["entities"][0]["version"] = "9.9.9"
        index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

        check = publish_mod.verify_index_signature(
            str(root), str(root / "keys" / f"{key_id}.pub")
        )

        assert check["ok"] is False
        assert check["reason"] in ("index_changed", "signature_invalid")

    def test_a_missing_signature_is_reported_not_raised(self, tmp_path):
        root = tmp_path / "catalog"
        root.mkdir(parents=True)
        (root / "index.json").write_text(
            json.dumps({"schema": publish_mod.INDEX_SCHEMA, "entities": []}),
            encoding="utf-8",
        )

        check = publish_mod.verify_index_signature(
            str(root), str(tmp_path / "nope.pub")
        )

        assert check["ok"] is False
        assert check["reason"] == "signature_missing"

    def test_the_private_key_is_not_world_readable(self, tmp_path):
        private = tmp_path / "private" / "m.pem"
        publish_mod.generate_keypair(
            str(private), str(tmp_path / "keys" / "k.pub"), "k"
        )

        mode = os.stat(private).st_mode & 0o777

        assert mode == 0o600


# ─── CLI ──────────────────────────────────────────────────────────────────

def test_the_cli_publishes_a_folder(tmp_path):
    from scripts.marketplace_publish import main

    root = tmp_path / "catalog"
    folder = _write_folder(tmp_path / "src", SKILL_FILES)
    manifest_path = tmp_path / "entity.json"
    manifest_path.write_text(
        json.dumps({"name": {"en": "Demo"}}, ensure_ascii=False), encoding="utf-8"
    )

    status = main([
        "--root", str(root), "--type", "skill", "--id", "demo_skill",
        "--version", "1.0.0", "--path", str(folder),
        "--manifest", str(manifest_path),
    ])

    assert status == 0
    assert (root / "index.json").is_file()
    entity = json.loads(
        (root / "skills/demo_skill/1.0.0/entity.json").read_text(encoding="utf-8")
    )
    assert entity["name"] == {"en": "Demo"}
    assert entity["id"] == "demo_skill"


def test_a_helper_agent_publishes_as_its_own_kind(tmp_path):
    """An assistant package publishes under ``agent`` with its own kind.

    The kind is what separates an employee (a folder of instructions) from
    a helper (a profile with a prompt): the showcase splits them into two
    subsections, so the value has to survive the publish and reach the
    index entry without downloading the package.
    """
    root = tmp_path / "catalog"
    folder = _write_folder(tmp_path / "helper", {
        "manifest.json": json.dumps({"name": "Meeting Minutes", "service": "DeepSeek"}),
        "prompt.md": "## Role\nWrite minutes.\n",
    })

    report = publish_mod.publish(
        str(root), str(folder),
        {
            "type": "agent",
            "agent_kind": "assistant",
            "id": "meeting_minutes",
            "version": "1.0.0",
            "name": {"en": "Meeting Minutes"},
        },
    )

    entity = json.loads(
        (root / "agents/meeting_minutes/1.0.0/entity.json").read_text(encoding="utf-8")
    )
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    entry = [item for item in index["entities"] if item["id"] == "meeting_minutes"][0]

    assert report["changed"] is True
    assert entity["agent_kind"] == "assistant"
    assert sorted(entity["files"]) == ["manifest.json", "prompt.md"]
    assert entry["agent_kind"] == "assistant"
    assert entry["path"] == "agents/meeting_minutes/1.0.0"


def test_a_recommended_package_survives_the_publish(tmp_path):
    """``recommends`` reaches the served manifest unchanged.

    The card reads its package members from the published manifest, so a
    field dropped by the publisher would leave every card without its
    "Extras" block while the author's build looked correct.
    """
    root = tmp_path / "catalog"
    folder = _write_folder(tmp_path / "agent", AGENT_FILES)
    recommends = [{"type": "rag", "id": "demo_base", "default_selected": True}]

    publish_mod.publish(
        str(root), str(folder),
        {
            "type": "agent",
            "agent_kind": "orchestrator",
            "id": "demo_agent",
            "version": "2.11.1",
            "name": {"en": "Demo"},
            "recommends": recommends,
        },
    )

    entity = json.loads(
        (root / "agents/demo_agent/latest/entity.json").read_text(encoding="utf-8")
    )

    assert entity["recommends"] == recommends


# ─── Standalone export ────────────────────────────────────────────────────

class TestStandaloneExport:
    def test_the_export_compiles_without_the_platform(self, tmp_path):
        """The catalog copy must run in a clone with no SagaAI next to it."""
        from scripts.marketplace_publish import export_standalone

        target = tmp_path / "publish.py"
        info = export_standalone(str(target))
        source = target.read_text(encoding="utf-8")

        compile(source, str(target), "exec")
        assert "core.marketplace" not in source
        assert "export_standalone" not in source
        assert info["sha256"] == publish_mod.sha256_bytes(source.encode("utf-8"))

    def test_the_exported_copy_publishes_a_catalog(self, tmp_path):
        """End to end in a clean cwd: the export builds a readable catalog."""
        import subprocess

        from scripts.marketplace_publish import export_standalone

        script = tmp_path / "publish.py"
        export_standalone(str(script))
        folder = _write_folder(tmp_path / "src", SKILL_FILES)
        root = tmp_path / "catalog"
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

        result = subprocess.run(
            [sys.executable, str(script), "--root", str(root), "--type", "skill",
             "--id", "demo_skill", "--version", "1.0.0", "--path", str(folder)],
            capture_output=True, text=True, cwd=str(tmp_path), env=env, timeout=120,
        )

        assert result.returncode == 0, result.stderr
        assert (root / "index.json").is_file()
        entity = json.loads(
            (root / "skills/demo_skill/1.0.0/entity.json").read_text(encoding="utf-8")
        )
        assert entity["id"] == "demo_skill"
        assert entity["package"]["sha256"]
        assert (root / "skills/demo_skill/1.0.0/package.tar.gz").is_file()
