"""
Run history retention and undoing a marketplace run (P3).

Two separate promises are checked here. The journal must stay bounded:
skills keep twenty runs, RAG bases three, because a knowledge base run is
an order of magnitude heavier than a skill run. And a finished run must be
reversible: undo restores the files, the registry row and ``skills.json``
byte for byte, which is what the "Undo this run" button promises.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    add_catalog_version,
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)


# ─── Helpers ──────────────────────────────────────────────────────────────

def _settings(monkeypatch, **overrides):
    """Patch the marketplace settings for one test."""
    from core.marketplace import config as marketplace_config

    values = {
        "enabled": True,
        "base_url": "",
        "allow_code": True,
        "require_signature": False,
        "channel": "main",
    }
    values.update(overrides)
    monkeypatch.setattr(marketplace_config, "marketplace_settings", lambda: dict(values))
    return values


def _install(base_url, entity):
    """Install one entity through the real installer."""
    from core.marketplace.installer import install_skill
    from core.marketplace.source import MarketplaceSource

    return install_skill(entity, channel="main", source=MarketplaceSource(base_url))


def _source(base_url):
    """Return a catalog transport bound to *base_url*."""
    from core.marketplace.source import MarketplaceSource

    return MarketplaceSource(base_url)


def _skills_json() -> Path:
    """Return the path of the skills-library registry file."""
    from core.skills_library import _registry_path

    return Path(_registry_path())


# ─── Retention ────────────────────────────────────────────────────────────

def _run(type_: str, index: int) -> dict:
    """Build one synthetic run record of the given type."""
    return {
        "install_id": f"2026010{index % 9}T00{index % 60:02d}00Z-{index:04x}",
        "type": type_,
        "id": f"entity_{index}",
        "action": "install",
        "status": "done",
        "started_at": f"2026-01-{index % 28 + 1:02d}T00:00:00Z",
    }


def test_a_skill_keeps_the_twenty_newest_runs(marketplace_data_dir):
    from core.marketplace import store

    records = [_run("skill", index) for index in range(21)]

    kept = store.prune_history(records)

    assert len(kept) == 20
    # The oldest run is the one that falls out; the order is chronological.
    assert kept[0]["install_id"] == records[1]["install_id"]
    assert kept[-1]["install_id"] == records[-1]["install_id"]


def test_a_rag_base_keeps_only_three_runs(marketplace_data_dir):
    from core.marketplace import store

    records = [_run("rag", index) for index in range(4)]

    kept = store.prune_history(records)

    assert len(kept) == 3
    assert kept[0]["install_id"] == records[1]["install_id"]


def test_types_are_pruned_independently(marketplace_data_dir):
    """A flood of skill runs must not evict the RAG journal."""
    from core.marketplace import store

    records = [_run("rag", index) for index in range(3)]
    records.extend(_run("skill", index) for index in range(25))

    kept = store.prune_history(records)

    assert sum(1 for record in kept if record["type"] == "rag") == 3
    assert sum(1 for record in kept if record["type"] == "skill") == 20


def test_saving_history_applies_the_same_limits(marketplace_data_dir):
    """The limit is part of the write path, not something a caller adds."""
    from core.marketplace import store

    store.save_history([_run("skill", index) for index in range(26)])

    stored = store.load_history()
    assert len(stored) == 20
    assert stored[-1]["id"] == "entity_25"


# ─── Undo ─────────────────────────────────────────────────────────────────

def test_undoing_an_update_restores_files_row_and_skills_json(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    """One run, four pieces of state, all of them back where they were."""
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)

    from core.marketplace import registry, rollback, store, updates

    folder = Path(store.data_dir()) / "skills" / "demo_skill"
    registry_before = _skills_json().read_bytes()
    files_before = {
        path: path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }

    newer, newer_package = make_entity(
        version="1.1.0",
        files={
            "SKILL.md": "# demo_skill v1.1\n",
            "references/notes.md": "v1.1 notes\n",
            "scripts/run.py": "print('demo v1.1')\n",
        },
    )
    add_catalog_version(catalog_root, newer, newer_package)
    report = updates.apply_entity_updates(
        ["demo_skill"], "keep_mine", source=_source(base_url)
    )
    install_id = report["applied"][0]["install_id"]
    assert (folder / "SKILL.md").read_text(encoding="utf-8") == "# demo_skill v1.1\n"

    undo = rollback.rollback_run(install_id, reason="user")

    # 1. Files are byte-identical to the pre-update state.
    assert undo["ok"] is True
    files_after = {
        path: path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }
    assert files_after == files_before
    # 2. The registry row is back to the installed version.
    row = registry.get_installed("skill", "demo_skill")
    assert row["version"] == "1.0.0"
    # 3. ``skills.json`` is byte-identical.
    assert _skills_json().read_bytes() == registry_before
    # 4. The journal entry says the run was undone.
    entry = store.find_run(install_id)
    assert entry["status"] == "rolled_back"
    assert entry["rollback_reason"] == "user"


def test_the_journal_records_the_run_before_the_files_change(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    """An attempt that fails mid-way must still be visible in the journal."""
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)

    from core.marketplace import installer, store, verify

    def _broken(*args, **kwargs):
        raise verify.VerifyError("package_hash_mismatch", "simulated corruption")

    monkeypatch.setattr(verify, "verify_package", _broken)

    with pytest_raises_install_error():
        _install(base_url, entity)

    records = store.load_history()
    assert records, "a failed install must leave a journal entry"
    assert records[-1]["status"] == "failed"
    assert records[-1]["rolled_back"] is False


def pytest_raises_install_error():
    """Context manager expecting ``installer.InstallError``.

    The installer wraps a failed verification into its own error type, and
    the test only cares that the run failed - not which layer reported it.
    """
    from contextlib import contextmanager

    from core.marketplace import installer

    @contextmanager
    def _ctx():
        try:
            yield
        except installer.InstallError:
            return
        raise AssertionError("the install should have failed")

    return _ctx()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
