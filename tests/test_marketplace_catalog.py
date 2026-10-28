"""
test_marketplace_catalog.py - the read model behind the showcase.

The page paints what this module returns, so the rules it encodes are the
visible behaviour of the marketplace: which cards appear, how a name is
localised, and how the installed state is decided. None of that needs
Streamlit, which is why it can be tested here directly.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    free_port,
    make_catalog,
    make_entity,
    make_installed_row,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)


class TestListSkills:
    def test_lists_skill_entries(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        catalog_root = make_catalog(tmp_path / "catalog", [
            make_entity(entity_id="first_skill"),
            make_entity(entity_id="second_skill"),
        ])
        answer = catalog.list_skills(source=_source(serve_catalog(catalog_root)))

        assert answer["ok"] is True
        assert [item["id"] for item in answer["items"]] == ["first_skill", "second_skill"]
        assert answer["installed"] == 0

    def test_skips_other_entity_types(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        skill, package = make_entity(entity_id="a_skill")
        agent, agent_package = make_entity(entity_id="an_agent", type_="agent")
        catalog_root = make_catalog(tmp_path / "catalog", [
            (skill, package),
            (agent, agent_package),
        ])
        answer = catalog.list_skills(source=_source(serve_catalog(catalog_root)))
        assert [item["id"] for item in answer["items"]] == ["a_skill"]

    def test_localises_names(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        catalog_root = make_catalog(tmp_path / "catalog", [make_entity()])
        base_url = serve_catalog(catalog_root)
        ru = catalog.list_skills(source=_source(base_url), lang="ru")
        en = catalog.list_skills(source=_source(base_url), lang="en")
        assert ru["items"][0]["name"] == "Демонстрационный навык demo_skill"
        assert en["items"][0]["name"] == "Demo Skill"

    def test_marks_installed_entries(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        catalog_root = make_catalog(tmp_path / "catalog", [
            make_entity(entity_id="installed_skill"),
            make_entity(entity_id="free_skill"),
        ])
        make_installed_row("installed_skill", version="1.0.0")

        answer = catalog.list_skills(source=_source(serve_catalog(catalog_root)))
        flags = {item["id"]: item["installed"] for item in answer["items"]}
        assert flags == {"installed_skill": True, "free_skill": False}
        assert answer["installed"] == 1
        # Installed entries sort last so the catalog offers new things first.
        assert answer["items"][-1]["id"] == "installed_skill"

    def test_reports_catalog_failure(self, marketplace_data_dir):
        from core.marketplace import catalog

        answer = catalog.list_skills(source=_source(f"http://127.0.0.1:{free_port()}"))
        assert answer["ok"] is False
        assert answer["items"] == []
        assert answer["error_code"]

    def test_marks_stale_index(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        catalog_root = make_catalog(tmp_path / "catalog", [make_entity()])
        base_url = serve_catalog(catalog_root)
        catalog.list_skills(source=_source(base_url))

        # The second call goes to a dead port; the cached index must carry
        # the staleness marker so the page can say the data may be old.
        offline = catalog.list_skills(source=_source(f"http://127.0.0.1:{free_port()}"))
        assert offline["ok"] is True
        assert offline["stale"] is True
        assert offline["items"][0]["id"] == "demo_skill"


class TestCard:
    def test_states_of_a_manifest(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        entity, package = make_entity(version="2.0.0")
        catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
        base_url = serve_catalog(catalog_root)

        fresh = catalog.fetch_card("demo_skill", source=_source(base_url))
        assert fresh["ok"] is True
        assert fresh["state"] == "not_installed"

        make_installed_row("demo_skill", version="2.0.0")
        current = catalog.fetch_card("demo_skill", source=_source(base_url))
        assert current["state"] == "installed"

        make_installed_row("demo_skill", version="1.0.0")
        outdated = catalog.fetch_card("demo_skill", source=_source(base_url))
        assert outdated["state"] == "update_available"
        assert outdated["installed_version"] == "1.0.0"

    def test_card_states_permissions_and_licence(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        entity, package = make_entity(
            license_spdx="PolyForm-Noncommercial-1.0.0",
            license_acceptance_required=True,
            requires_tools=["web_search", "ssh_exec"],
            executes_code=True,
        )
        catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
        view = catalog.fetch_card("demo_skill", source=_source(serve_catalog(catalog_root)))

        assert view["license_spdx"] == "PolyForm-Noncommercial-1.0.0"
        assert view["acceptance_required"] is True
        assert view["executes_code"] is True
        assert view["requires_tools"] == ["web_search", "ssh_exec"]
        assert view["file_count"] == len(entity["files"])
        assert view["size_bytes"] == entity["package"]["size"]

    def test_card_reports_fetch_failure(self, marketplace_data_dir):
        from core.marketplace import catalog

        view = catalog.fetch_card("absent", source=_source(f"http://127.0.0.1:{free_port()}"))
        assert view["ok"] is False
        assert view["error_code"]

    def test_card_exposes_manifest_for_installer(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        entity, package = make_entity()
        catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
        view = catalog.fetch_card("demo_skill", source=_source(serve_catalog(catalog_root)))
        # The page hands this manifest straight to the installer, so it must
        # be the untouched catalog payload.
        assert view["entity"]["package"]["sha256"] == entity["package"]["sha256"]


class TestLocalized:
    @pytest.mark.parametrize("value,lang,expected", [
        ({"en": "Name", "ru": "Имя"}, "ru", "Имя"),
        ({"en": "Name", "ru": "Имя"}, "zh-CN", "Name"),
        ({"ru": "Имя"}, "en", "Имя"),
        ({"en": "", "ru": "Имя"}, "en", "Имя"),
        ("Plain", "ru", "Plain"),
        (None, "ru", ""),
        ({}, "ru", ""),
    ])
    def test_fallbacks(self, value, lang, expected):
        from core.marketplace.catalog import localized

        assert localized(value, lang) == expected


def _source(base_url: str):
    """Return a catalog source bound to *base_url*."""
    from core.marketplace.source import MarketplaceSource

    return MarketplaceSource(base_url)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
