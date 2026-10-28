"""
Updates scenario - what a user sees on the Updates page (P3).

The page is paint over ``core.marketplace.updates``: it asks for a check
only when the button is pressed, lists what came back, and passes the
chosen policy to the update run. These tests walk that path the way a
user does - press check, read the row, press update - and then assert on
the files on disk, because a button that reports success without moving a
byte is the failure mode that matters.
"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import StopRerun, install_streamlit_mock  # noqa: E402
from tests.marketplace_fixtures import (  # noqa: E402
    add_catalog_version,
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)


def _settings(monkeypatch, base_url: str, **overrides):
    """Point the marketplace at the local catalog for one test."""
    from core.marketplace import config as marketplace_config

    values = {
        "enabled": True,
        "base_url": base_url,
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


def _fresh_page_modules():
    """Re-import the Updates page so it binds to the current mock."""
    for name in list(sys.modules):
        if name.startswith("ui.pages.updates"):
            sys.modules.pop(name, None)
    return (
        importlib.import_module("ui.pages.updates"),
        importlib.import_module("ui.pages.updates_marketplace"),
    )


def _render(monkeypatch, base_url, clicks=(), state=None):
    """Render the Updates page and return the Streamlit mock.

    ``clicks`` seeds the buttons pressed in this pass and ``state`` carries
    the session state of the previous one, so a two-step interaction is two
    renders - exactly like a real press followed by a rerun.
    """
    _settings(monkeypatch, base_url)
    with install_streamlit_mock() as mock:
        mock.session_state["ui_lang"] = "en"
        mock.session_state.update(dict(state or {}))
        mock.click(*clicks)
        page_updates, _sections = _fresh_page_modules()
        try:
            page_updates.page_updates()
        except StopRerun:
            pass
    return mock


def _marks(mock) -> list:
    """Return every markdown line the page painted."""
    return [str(args[0]) for name, args, _kw in mock.calls if name == "markdown" and args]


def _folder(slug: str = "demo_skill") -> Path:
    """Return the installed skill folder of the current test."""
    from core.marketplace import store

    return Path(store.data_dir()) / "skills" / slug


V2_FILES = {
    "SKILL.md": "# demo_skill v2\n",
    "references/notes.md": "v2 notes\n",
    "scripts/run.py": "print('demo v2')\n",
}


# ─── The visible row ──────────────────────────────────────────────────────

def test_the_page_shows_the_available_update_after_a_check(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)
    newer, newer_package = make_entity(version="1.1.0", files=V2_FILES)
    add_catalog_version(catalog_root, newer, newer_package)

    mock = _render(monkeypatch, base_url, clicks=("mp_upd_check",))

    marks = "\n".join(_marks(mock))
    assert "1.0.0 → 1.1.0" in marks
    # Nothing is installed before the user presses update.
    assert (_folder() / "SKILL.md").read_text(encoding="utf-8").startswith("# demo_skill")
    assert "v2" not in (_folder() / "SKILL.md").read_text(encoding="utf-8")


def test_the_page_marks_an_edited_installation_and_updates_the_rest(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)
    (_folder() / "SKILL.md").write_text("my own words", encoding="utf-8")

    newer, newer_package = make_entity(version="2.0.0", files=V2_FILES)
    add_catalog_version(catalog_root, newer, newer_package)

    first = _render(monkeypatch, base_url, clicks=("mp_upd_check",))
    marks = "\n".join(_marks(first))
    assert "2.0.0" in marks
    assert "changed by you" in marks

    # The radio picks its first option, which is "keep my edits".
    second = _render(
        monkeypatch,
        base_url,
        clicks=("mp_upd_apply_skill_demo_skill",),
        state=dict(first.session_state),
    )

    assert second.rerun_count == 1
    assert (_folder() / "SKILL.md").read_text(encoding="utf-8") == "my own words"
    assert (_folder() / "references" / "notes.md").read_text(encoding="utf-8") == "v2 notes\n"
    from core.marketplace import registry

    assert registry.get_installed("skill", "demo_skill")["user_modified"] is True


def test_the_page_reports_a_platform_refusal_instead_of_offering_the_update(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)
    newer, newer_package = make_entity(version="2.0.0", platform={"min": "99.0.0"})
    add_catalog_version(catalog_root, newer, newer_package)

    mock = _render(monkeypatch, base_url, clicks=("mp_upd_check",))

    warnings = [str(args[0]) for name, args, _kw in mock.calls if name == "warning" and args]
    assert any("99.0.0" in text for text in warnings)
    keys = [call[2].get("key") for call in mock.calls if call[0] == "button"]
    assert "mp_upd_apply_skill_demo_skill" not in keys


def test_the_sections_list_updates_of_every_installed_type(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    """An agent and a skill both land in their own section and both update.

    The page used to read only the skill section, so a newer agent release
    was invisible there even though the catalog knew about it.
    """
    from core.marketplace import registry
    from core.marketplace.installer import install_entity
    from tests.marketplace_fixtures import make_installed_row

    skill, skill_package = make_entity(version="1.0.0")
    agent, agent_package = make_entity(
        entity_id="demo_agent", version="1.0.0", type_="agent",
        files={
            "orchestrator.json": (
                '{"format": "sagaai_orchestrator/v1", "slug": "demo_agent",'
                ' "name": "Demo Agent", "config": {}, "tools": []}'
            ),
            "system_prompt.md": "You are a demo agent.\n",
        },
        extra={"agent_kind": "orchestrator"},
    )
    catalog_root = make_catalog(
        tmp_path / "catalog", [(skill, skill_package), (agent, agent_package)]
    )
    base_url = serve_catalog(catalog_root)

    _install(base_url, skill)
    make_installed_row("demo_agent", type_="agent", version="1.0.0")

    newer_skill, newer_skill_package = make_entity(version="2.0.0", files=V2_FILES)
    add_catalog_version(catalog_root, newer_skill, newer_skill_package)

    first = _render(monkeypatch, base_url, clicks=("mp_upd_check",))
    marks = "\n".join(_marks(first))
    assert "Skills" in marks
    assert "Agents" in marks
    assert "1.0.0 → 2.0.0" in marks

    keys = [call[2].get("key") for call in first.calls if call[0] == "button"]
    assert "mp_upd_apply_skill_demo_skill" in keys


# ─── The journal ──────────────────────────────────────────────────────────

def test_the_journal_offers_the_undo_of_a_finished_install(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    installed = _install(base_url, entity)
    install_id = installed["install_id"]
    folder = _folder()
    assert folder.is_dir()

    first = _render(monkeypatch, base_url)
    keys = [call[2].get("key") for call in first.calls if call[0] == "button"]
    assert f"mp_upd_undo_{install_id}" in keys

    second = _render(
        monkeypatch,
        base_url,
        clicks=(f"mp_upd_undo_{install_id}",),
        state=dict(first.session_state),
    )

    assert second.rerun_count == 1
    assert not folder.exists()
    from core.marketplace import registry, store

    assert registry.get_installed("skill", "demo_skill") is None
    assert store.find_run(install_id)["status"] == "rolled_back"


# ─── The switch ───────────────────────────────────────────────────────────

def test_a_disabled_marketplace_paints_nothing_on_the_updates_page(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)

    _settings(monkeypatch, base_url, enabled=False)
    with install_streamlit_mock() as mock:
        mock.session_state["ui_lang"] = "en"
        page_updates, _sections = _fresh_page_modules()
        try:
            page_updates.page_updates()
        except StopRerun:
            pass

    keys = [call[2].get("key") for call in mock.calls if call[0] == "button"]
    assert "mp_upd_check" not in keys
    subheaders = [str(args[0]) for name, args, _kw in mock.calls if name == "subheader" and args]
    assert not any("Marketplace entities" in text for text in subheaders)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
