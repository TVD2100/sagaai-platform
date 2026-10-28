"""
test_marketplace_catalog_types.py - the four-type read model of the showcase.

The showcase paints agents, skills, RAG bases and MCP servers from one
catalog. These tests pin the rules that make that safe: a type filter
returns only its own entities, the unfiltered list tags every card with its
type, and an installation is looked up per type so two entities may share
an id without shadowing each other.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    free_port,
    make_catalog,
    make_entity,
    make_installed_row,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)

TYPES = ("agent", "skill", "rag", "mcp")


def _source(base_url: str):
    """Return a catalog source bound to *base_url*."""
    from core.marketplace.source import MarketplaceSource

    return MarketplaceSource(base_url)


def _catalog_with_one_of_each(tmp_path, serve_catalog):
    """Serve a catalog holding one entity of every type."""
    entries = []
    for type_ in TYPES:
        entity, package = make_entity(entity_id=f"demo_{type_}", type_=type_)
        entries.append((entity, package))
    root = make_catalog(tmp_path / "catalog", entries)
    return _source(serve_catalog(root))


def _catalog_with_two_agent_kinds(tmp_path, serve_catalog):
    """Serve one employee and one helper, both of type ``agent``."""
    entries = []
    for entity_id, kind in (("demo_employee", "orchestrator"), ("demo_helper", "assistant")):
        entity, package = make_entity(
            entity_id=entity_id, type_="agent", extra={"agent_kind": kind}
        )
        entries.append((entity, package))
    root = make_catalog(tmp_path / "catalog", entries)
    return _source(serve_catalog(root))


class TestAgentKind:
    def test_the_card_names_the_kind_of_an_agent(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import catalog

        answer = catalog.list_entities("agent", source=_catalog_with_two_agent_kinds(tmp_path, serve_catalog))
        kinds = {item["id"]: item["agent_kind"] for item in answer["items"]}

        assert kinds == {"demo_employee": "orchestrator", "demo_helper": "assistant"}

    def test_a_manifest_without_a_kind_is_an_employee(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import catalog

        source = _catalog_with_one_of_each(tmp_path, serve_catalog)
        answer = catalog.list_entities("agent", source=source)

        assert answer["items"][0]["agent_kind"] == "orchestrator"

    def test_other_types_carry_no_kind(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        source = _catalog_with_one_of_each(tmp_path, serve_catalog)
        for type_ in ("skill", "rag", "mcp"):
            answer = catalog.list_entities(type_, source=source)
            assert answer["items"][0]["agent_kind"] == ""

    def test_the_full_card_repeats_the_kind(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        source = _catalog_with_two_agent_kinds(tmp_path, serve_catalog)
        entity = source.fetch_entity("agent", "demo_helper")
        view = catalog.card(entity)

        assert view["agent_kind"] == "assistant"


class TestTypeFilter:
    def test_each_type_returns_its_own_entities(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        source = _catalog_with_one_of_each(tmp_path, serve_catalog)
        for type_ in TYPES:
            answer = catalog.list_entities(type_, source=source)
            assert answer["ok"] is True
            assert [item["id"] for item in answer["items"]] == [f"demo_{type_}"]
            assert answer["items"][0]["type"] == type_

    def test_all_types_come_back_tagged(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        answer = catalog.list_entities(None, source=_catalog_with_one_of_each(tmp_path, serve_catalog))
        assert answer["ok"] is True
        assert sorted(item["type"] for item in answer["items"]) == sorted(TYPES)
        assert answer["by_type"] == {type_: 1 for type_ in TYPES}

    def test_unknown_type_returns_an_empty_list(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        source = _catalog_with_one_of_each(tmp_path, serve_catalog)
        answer = catalog.list_entities("plugin", source=source)
        assert answer["ok"] is True
        assert answer["items"] == []
        assert answer["by_type"] == {}

    def test_skill_only_helper_still_works(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        source = _catalog_with_one_of_each(tmp_path, serve_catalog)
        answer = catalog.list_skills(source=source)
        assert [item["id"] for item in answer["items"]] == ["demo_skill"]


class TestInstalledPerType:
    def test_same_id_in_two_types_is_not_shared(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        shared_id = "shared_name"
        entries = []
        for type_ in ("skill", "agent"):
            entity, package = make_entity(entity_id=shared_id, type_=type_)
            entries.append((entity, package))
        root = make_catalog(tmp_path / "catalog", entries)
        make_installed_row(shared_id, type_="agent")

        source = _source(serve_catalog(root))
        skills = catalog.list_entities("skill", source=source)["items"]
        agents = catalog.list_entities("agent", source=source)["items"]
        assert skills[0]["installed"] is False
        assert agents[0]["installed"] is True

    def test_unfiltered_list_keeps_installations_apart(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        shared_id = "shared_name"
        entries = []
        for type_ in ("skill", "agent"):
            entity, package = make_entity(entity_id=shared_id, type_=type_)
            entries.append((entity, package))
        root = make_catalog(tmp_path / "catalog", entries)
        make_installed_row(shared_id, type_="agent")

        answer = catalog.list_entities(None, source=_source(serve_catalog(root)))
        flags = {item["type"]: item["installed"] for item in answer["items"]}
        assert flags == {"agent": True, "skill": False}
        assert answer["installed"] == 1


class TestCardTypes:
    def test_card_reports_its_type(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        entity, package = make_entity(entity_id="demo_rag", type_="rag")
        root = make_catalog(tmp_path / "catalog", [(entity, package)])
        view = catalog.fetch_card(
            "demo_rag", source=_source(serve_catalog(root)), type_="rag"
        )
        assert view["ok"] is True
        assert view["type"] == "rag"

    def test_card_state_uses_the_type_row(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        entity, package = make_entity(entity_id="demo_agent", type_="agent", version="2.0.0")
        root = make_catalog(tmp_path / "catalog", [(entity, package)])
        source = _source(serve_catalog(root))

        assert catalog.fetch_card("demo_agent", source=source, type_="agent")["state"] == "not_installed"
        make_installed_row("demo_agent", type_="agent", version="2.0.0")
        assert catalog.fetch_card("demo_agent", source=source, type_="agent")["state"] == "installed"

    def test_card_lists_declared_secrets(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        entity, package = make_entity(entity_id="demo_mcp", type_="mcp")
        entity["permissions"] = {"executes_code": False, "secrets": ["GITHUB_TOKEN"]}
        root = make_catalog(tmp_path / "catalog", [(entity, package)])
        view = catalog.fetch_card(
            "demo_mcp", source=_source(serve_catalog(root)), type_="mcp"
        )
        assert view["requires_secrets"] == ["GITHUB_TOKEN"]

    def test_prebuilt_base_reports_index_size_and_sources(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        """A base with only a prebuilt index still has a size and a volume.

        Such a package ships no files at all, so the card would otherwise
        show ``0 files · 0 B`` for a base of several megabytes - the very
        numbers a user compares before pressing install.
        """
        from core.marketplace import catalog

        entity, package = make_entity(entity_id="demo_rag", type_="rag", files={})
        entity["distribution"] = {
            "mode": "prebuilt_index",
            "chunks": 2013,
            "sources_count": 1703,
            "index_bytes": 7258112,
        }
        root = make_catalog(tmp_path / "catalog", [(entity, package)])
        view = catalog.fetch_card(
            "demo_rag", source=_source(serve_catalog(root)), type_="rag"
        )

        assert view["file_count"] == 1703
        # The download is the package with the manifest plus the index itself.
        assert view["size_bytes"] == entity["package"]["size"] + 7258112
        assert view["distribution"]["chunks"] == 2013


class TestBundleNormalization:
    def test_bare_strings_become_records(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog

        entity, package = make_entity(
            entity_id="demo_agent",
            type_="agent",
            extra={"recommends": ["a_skill", "a_rag"], "bundle": ["a_skill"]},
        )
        root = make_catalog(tmp_path / "catalog", [(entity, package)])
        view = catalog.fetch_card(
            "demo_agent", source=_source(serve_catalog(root)), type_="agent"
        )
        assert view["recommends"] == [
            {"id": "a_skill", "type": "", "default_selected": True},
            {"id": "a_rag", "type": "", "default_selected": True},
        ]
        assert view["bundle"] == [{"id": "a_skill", "type": "", "default_selected": True}]

    def test_records_keep_type_and_default_flag(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import catalog
        from core.marketplace.catalog import _bundle

        members = _bundle({"bundle": [
            {"id": "a_skill", "type": "skill", "default_selected": True},
            {"id": "a_rag", "type": "rag", "default_selected": False},
            {"id": "no_type"},
        ]})
        assert members == [
            {"id": "a_skill", "type": "skill", "default_selected": True},
            {"id": "a_rag", "type": "rag", "default_selected": False},
            {"id": "no_type", "type": "", "default_selected": True},
        ]

    def test_missing_bundle_is_empty(self):
        from core.marketplace.catalog import _bundle, _recommends

        assert _bundle({}) == []
        assert _recommends({}) == []
        assert _bundle({"bundle": "not-a-list"}) == []


class TestCatalogFailure:
    def test_failure_reports_by_type(self, marketplace_data_dir):
        from core.marketplace import catalog

        answer = catalog.list_entities(None, source=_source(f"http://127.0.0.1:{free_port()}"))
        assert answer["ok"] is False
        assert answer["by_type"] == {}
        assert answer["error_code"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
