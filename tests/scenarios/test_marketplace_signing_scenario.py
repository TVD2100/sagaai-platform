"""
Signed marketplace scenario - a release ends up installable, tampering does not.

Given the holder of the release key publishes a skill, the user on the
``stable`` channel sees it in the catalog and installs it. The scenario then
walks the failure side of the same path: an edited manifest, a catalog whose
index lost its signature and a client that trusts a different key are all
refused before a package is touched. Every step drives the real modules -
``publish``, ``MarketplaceSource``, ``installer`` - and asserts on what a
user can observe: an installed folder, or a refusal with a code.

The catalog is served from a ``file://`` folder, so the scenario needs no
network and no port.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.marketplace_fixtures import marketplace_data_dir  # noqa: E402,F401


def _release_pair(base, name="release", keyring_name="keyring"):
    """Create a release key and return (private path, keyring folder, key id)."""
    from core import signing

    keyring = base / keyring_name
    keyring.mkdir(parents=True, exist_ok=True)
    private = base / f"{name}.pem"
    public = keyring / f"{name}.pem"
    report = signing.generate_keypair(str(private), str(public))
    return private, keyring, report["key_id"]


def _entity_folder(tmp_path, text="# Demo skill\n", name="source"):
    """Create the source folder of a skill and return its path."""
    folder = tmp_path / name
    (folder / "references").mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    (folder / "references" / "notes.md").write_text("notes\n", encoding="utf-8")
    return folder


def _manifest(entity_id, name="Signed skill"):
    """Return the descriptive part of an entity manifest."""
    return {
        "id": entity_id,
        "version": "1.0.0",
        "type": "skill",
        "name": name,
        "description": "A skill published through the signed path.",
        "license": "MIT",
    }


def _publish(root, private, source_folder, entity_id="signed_skill"):
    """Publish one skill version into *root* and return its manifest."""
    from core.marketplace import publish

    manifest, _package = publish.build_manifest(
        _manifest(entity_id), publish.collect_files(str(source_folder)), "package.tar.gz"
    )
    publish.publish(
        str(root),
        str(source_folder),
        manifest,
        private_key_path=str(private),
        channel="stable",
    )
    return manifest


def _trust(monkeypatch, keyring, channel="stable"):
    """Point the marketplace at a keyring and a channel for one test."""
    from core.marketplace import attest

    monkeypatch.setattr(
        attest,
        "_settings",
        lambda: {"channel": channel, "signature_keyring": str(keyring)},
    )


def _source(root, channel="stable"):
    """Return a transport bound to a local catalog folder."""
    from core.marketplace.source import MarketplaceSource

    return MarketplaceSource("file://" + str(root), channel=channel)


def test_a_signed_release_is_visible_and_installs(tmp_path, marketplace_data_dir, monkeypatch):
    """Publish, read the catalog, install - the user gets a working skill."""
    from core.marketplace import registry
    from core.marketplace.installer import install_skill

    private, keyring, key_id = _release_pair(tmp_path)
    root = tmp_path / "catalog"
    manifest = _publish(root, private, _entity_folder(tmp_path))
    _trust(monkeypatch, keyring)

    # ── When: the client reads the catalog ──────────────────────────────
    source = _source(root)
    index = source.fetch_index()
    entity = source.fetch_entity("skill", "signed_skill", "1.0.0")

    # ── Then: the index and the manifest both carry the release key ─────
    assert source.signatures["index.json"]["verified"] is True
    assert source.signatures["skills/signed_skill/1.0.0/entity.json"]["verified"] is True
    assert source.signatures["index.json"]["key_id"] == key_id
    assert [entry["id"] for entry in index["entities"]] == ["signed_skill"]
    assert entity["package"]["sha256"] == manifest["package"]["sha256"]

    # ── When: the user installs it ──────────────────────────────────────
    result = install_skill(entity, channel="stable", source=source)

    # ── Then: the files and the registry row exist ──────────────────────
    folder = Path(marketplace_data_dir) / "skills" / result["runtime_slug"]
    assert (folder / "SKILL.md").is_file()
    assert (folder / "references" / "notes.md").is_file()
    assert registry.get_installed("skill", "signed_skill")["version"] == "1.0.0"


def test_an_edited_manifest_is_refused(tmp_path, marketplace_data_dir, monkeypatch):
    """One changed byte in a published manifest stops the client."""
    from core.marketplace.source import SourceError

    private, keyring, _key_id = _release_pair(tmp_path)
    root = tmp_path / "catalog"
    _publish(root, private, _entity_folder(tmp_path))
    _trust(monkeypatch, keyring)

    target = root / "skills" / "signed_skill" / "1.0.0" / "entity.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["description"] = "Rewritten after signing"
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(SourceError) as excinfo:
        _source(root).fetch_entity("skill", "signed_skill", "1.0.0")

    assert excinfo.value.code == "signature_refused"


def test_an_unsigned_index_is_refused_on_stable(tmp_path, marketplace_data_dir, monkeypatch):
    """A catalog that lost its index signature never reaches the installer."""
    from core.marketplace.source import SourceError

    private, keyring, _key_id = _release_pair(tmp_path)
    root = tmp_path / "catalog"
    _publish(root, private, _entity_folder(tmp_path))
    _trust(monkeypatch, keyring)
    (root / "index.json.sig").unlink()

    with pytest.raises(SourceError) as excinfo:
        _source(root).fetch_index()

    assert excinfo.value.code == "signature_refused"
    assert excinfo.value.details["reason"] == "signature_missing"


def test_a_warning_channel_reports_an_unsigned_index(tmp_path, marketplace_data_dir, monkeypatch):
    """On a development channel the same catalog is read and flagged."""
    private, keyring, _key_id = _release_pair(tmp_path)
    root = tmp_path / "catalog"
    _publish(root, private, _entity_folder(tmp_path))
    _trust(monkeypatch, keyring, channel="main")
    (root / "index.json.sig").unlink()

    source = _source(root, channel="main")
    index = source.fetch_index()

    assert index["entities"][0]["id"] == "signed_skill"
    assert source.signatures["index.json"]["verified"] is False
    assert source.signatures["index.json"]["policy"] == "warn"


def test_a_client_that_trusts_another_key_refuses_the_release(
    tmp_path, marketplace_data_dir, monkeypatch
):
    """A signature from an untrusted key is not a signature."""
    from core.marketplace.source import SourceError

    private, _keyring, _key_id = _release_pair(tmp_path)
    root = tmp_path / "catalog"
    _publish(root, private, _entity_folder(tmp_path))

    _private2, other_keyring, _other_id = _release_pair(
        tmp_path, name="other", keyring_name="other-keyring"
    )
    _trust(monkeypatch, other_keyring)

    with pytest.raises(SourceError) as excinfo:
        _source(root).fetch_index()

    assert excinfo.value.code == "signature_refused"
    assert excinfo.value.details["reason"] in {"unknown_key_id", "signature_invalid"}


def test_a_rotated_key_keeps_old_releases_readable(tmp_path, marketplace_data_dir, monkeypatch):
    """During a rotation both keys are trusted and the old release still reads."""
    private, keyring, first_id = _release_pair(tmp_path)
    root = tmp_path / "catalog"
    _publish(root, private, _entity_folder(tmp_path), entity_id="first_skill")

    # Both public halves live in one keyring: that is what a rotation is.
    second_private, rotated_keyring, second_id = _release_pair(tmp_path, name="release-2")
    assert rotated_keyring == keyring
    _trust(monkeypatch, keyring)

    # ── Then: the release signed before the rotation still verifies ──────
    source = _source(root)
    source.fetch_index()
    report = source.signatures["index.json"]

    assert report["verified"] is True
    assert report["key_id"] == first_id
    assert second_id != first_id

    # ── When: the new key publishes the next release ────────────────────
    from core.marketplace import publish

    second_folder = _entity_folder(tmp_path, "# Second skill\n", name="source-2")
    second_manifest, _package = publish.build_manifest(
        _manifest("second_skill", "Second skill"),
        publish.collect_files(str(second_folder)),
        "package.tar.gz",
    )
    publish.publish(
        str(root),
        str(second_folder),
        second_manifest,
        private_key_path=str(second_private),
        channel="stable",
    )

    rotated = _source(root)
    rotated.fetch_index()

    # ── Then: the index names the new key, the old entity stays readable ─
    assert rotated.signatures["index.json"]["verified"] is True
    assert rotated.signatures["index.json"]["key_id"] == second_id
    assert rotated.fetch_entity("skill", "first_skill", "1.0.0")["id"] == "first_skill"
    assert rotated.fetch_entity("skill", "second_skill", "1.0.0")["id"] == "second_skill"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
