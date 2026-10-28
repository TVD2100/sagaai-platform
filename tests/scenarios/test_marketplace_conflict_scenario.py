"""
Marketplace page scenario - what the user sees and presses.

The page is only paint over ``core.marketplace``, so these tests check the
wiring: a click on the card reaches the right module, the card reflects the
conflict plan, and the inline confirmation removes an entity only after
the second press. The Streamlit mock records widget calls and lets one
pass simulate a click, exactly like a real press followed by a rerun.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import StopRerun, install_streamlit_mock  # noqa: E402
from tests.marketplace_fixtures import (  # noqa: E402
    add_catalog_version,
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)


def _install(base_url, entity):
    from core.marketplace.installer import install_skill
    from core.marketplace.source import MarketplaceSource

    return install_skill(entity, channel="main", source=MarketplaceSource(base_url))


def _render(monkeypatch, base_url, clicks=(), state=None):
    """Render the page with the mock Streamlit and return the mock.

    ``clicks`` seeds the buttons that return True in this pass, so a single
    call reproduces "user pressed these buttons, then Streamlit reran".
    ``state`` carries the session state of a previous pass: a real rerun
    keeps it, while every mock starts empty.
    """
    from core.marketplace import config as marketplace_config

    monkeypatch.setattr(
        marketplace_config,
        "marketplace_settings",
        lambda: {
            "enabled": True,
            "base_url": base_url,
            "allow_code": True,
            "require_signature": False,
            "channel": "main",
        },
    )
    with install_streamlit_mock() as mock:
        mock.session_state["ui_lang"] = "en"
        # The showcase opens on its first subsection and these scenarios use
        # a skill: the pass starts where a sidebar click on Skills puts the
        # user, otherwise the card under test is not on screen at all.
        mock.session_state["mp_section"] = "skills"
        mock.session_state.update(dict(state or {}))
        mock.click(*clicks)
        # The page binds ``streamlit`` at import time, so a module cached by
        # an earlier test would keep the previous mock. Dropping it here makes
        # every pass bind to the mock installed above.
        sys.modules.pop("ui.pages.marketplace", None)
        from ui.pages.marketplace import page_marketplace

        try:
            page_marketplace()
        except StopRerun:
            pass
    return mock


def _prepared(tmp_path, serve_catalog):
    """Install 1.0.0, edit SKILL.md and publish 2.0.0."""
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)
    return entity, catalog_root, base_url


def _folder(data_dir, slug):
    return Path(data_dir) / "skills" / slug


def test_card_offers_the_three_paths_when_files_were_edited(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    entity, catalog_root, base_url = _prepared(tmp_path, serve_catalog)
    folder = _folder(marketplace_data_dir, "demo_skill")
    (folder / "SKILL.md").write_text("my own instructions", encoding="utf-8")
    newer, newer_package = make_entity(
        entity_id="demo_skill",
        version="2.0.0",
        files={"SKILL.md": "# v2 instructions\n", "references/notes.md": "v2 notes\n"},
    )
    add_catalog_version(catalog_root, newer, newer_package)

    mock = _render(monkeypatch, base_url, clicks=("mp_details_demo_skill",))

    button_labels = [args[0] for name, args, _kw in mock.calls if name == "button"]
    assert any("Keep my edits" == label for label in button_labels)
    assert any("Replace with the catalog version" == label for label in button_labels)
    assert any("Fork under a new name" == label for label in button_labels)
    # The card says why it asks: the files that differ are listed.
    assert mock.warnings


def test_keep_mine_keeps_the_user_file_and_updates_the_rest(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    entity, catalog_root, base_url = _prepared(tmp_path, serve_catalog)
    folder = _folder(marketplace_data_dir, "demo_skill")
    (folder / "SKILL.md").write_text("my own instructions", encoding="utf-8")
    newer, newer_package = make_entity(
        entity_id="demo_skill",
        version="2.0.0",
        files={"SKILL.md": "# v2 instructions\n", "references/notes.md": "v2 notes\n"},
    )
    add_catalog_version(catalog_root, newer, newer_package)

    mock = _render(
        monkeypatch,
        base_url,
        clicks=("mp_details_demo_skill", "mp_keep_demo_skill"),
    )

    assert mock.rerun_count == 1
    assert (folder / "SKILL.md").read_text(encoding="utf-8") == "my own instructions"
    assert (folder / "references/notes.md").read_text(encoding="utf-8") == "v2 notes\n"


def test_remove_asks_once_then_deletes(marketplace_data_dir, serve_catalog, tmp_path, monkeypatch):
    entity, catalog_root, base_url = _prepared(tmp_path, serve_catalog)
    folder = _folder(marketplace_data_dir, "demo_skill")
    from core.marketplace import registry

    # The card is shown on the same pass that presses Remove: the inline
    # confirmation appears, nothing is deleted yet.
    first = _render(monkeypatch, base_url, clicks=("mp_details_demo_skill", "mp_remove_demo_skill"))
    assert folder.is_dir()
    assert registry.get_installed("skill", "demo_skill") is not None
    assert str(first.session_state.get("mp_confirm_remove") or "") == "demo_skill"

    # The second pass presses only the confirmation: the card stays open
    # because its state was carried over from the first pass, exactly like a
    # real rerun keeps the session state.
    mock = _render(
        monkeypatch,
        base_url,
        clicks=("mp_remove_yes_demo_skill",),
        state=dict(first.session_state),
    )
    assert mock.rerun_count == 1
    assert not folder.exists()
    assert registry.get_installed("skill", "demo_skill") is None
    assert registry.is_removed("skill", "demo_skill") is True


def test_downgrade_warns_and_requires_a_confirmation(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    entity, package = make_entity(version="2.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)

    older, older_package = make_entity(version="1.0.0")
    older_catalog = make_catalog(tmp_path / "older", [(older, older_package)])
    older_url = serve_catalog(older_catalog)

    mock = _render(monkeypatch, older_url, clicks=("mp_details_demo_skill",))

    assert any("Installed 2.0.0" in str(msg) for msg in mock.warnings)
    labels = [args[0] for name, args, _kw in mock.calls if name == "checkbox"]
    assert any("I understand" in str(label) for label in labels)
    # Nothing was replaced behind the user's back.
    assert _folder(marketplace_data_dir, "demo_skill").joinpath("SKILL.md").is_file()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
