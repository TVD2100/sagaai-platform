"""
test_marketplace_bundles.py - curated sets installed as one run.

A bundle is the only place where several entities are installed by one
user action, so these tests concentrate on the two things that decide
whether it is safe: the plan a user sees before pressing the button, and
the all-or-nothing rollback when a later member fails. Members that were
already installed before the run are checked separately - undoing them
would delete an installation the run never made.
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
    write_bundles,
)


def _agent_entity(entity_id: str = "demo_agent", *, recommends=None, bundle=None,
                  version: str = "1.0.0"):
    """Build an agent manifest that carries a recommended package."""
    files = {
        "orchestrator.json": json.dumps({
            "format": "sagaai_orchestrator/v1",
            "slug": entity_id,
            "name": "Demo Agent",
            "config": {},
            "tools": [],
        }),
        "system_prompt.md": "You are a demo agent.\n",
    }
    extra = {"agent_kind": "orchestrator"}
    if recommends is not None:
        extra["recommends"] = recommends
    if bundle is not None:
        extra["bundle"] = bundle
    return make_entity(
        entity_id=entity_id,
        version=version,
        type_="agent",
        files=files,
        extra=extra,
    )


def _skill_entity(entity_id: str = "demo_skill", version: str = "1.0.0"):
    """Build a skill manifest with the default file set."""
    return make_entity(entity_id=entity_id, version=version, type_="skill")


def _serve(tmp_path, serve_catalog, entities, bundles=None):
    """Serve a catalog with *entities* (and optional bundles)."""
    root = make_catalog(tmp_path / "catalog", entities, bundles=bundles)
    return serve_catalog(root), root


def _source(base_url: str):
    """Return a catalog source bound to *base_url*."""
    from core.marketplace.source import MarketplaceSource

    return MarketplaceSource(base_url)


class TestPackagePlan:
    def test_merges_recommends_and_bundle_without_duplicates(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles

        skill, _ = _skill_entity("shared_skill")
        agent, _ = _agent_entity(
            recommends=[{"type": "skill", "id": "shared_skill"}],
            bundle=[{"type": "skill", "id": "shared_skill", "default_selected": False},
                    {"type": "skill", "id": "extra_skill", "default_selected": True}],
        )
        extra, _ = _skill_entity("extra_skill")
        base_url, _root = _serve(
            tmp_path, serve_catalog, [(agent, b""), (skill, b""), (extra, b"")]
        )

        # The packages above are rebuilt inside make_catalog, so fetch the
        # served manifest rather than the local copy.
        transport = _source(base_url)
        manifest = transport.fetch_entity("agent", "demo_agent", "1.0.0")
        result = bundles.plan(manifest, transport)

        ids = [item["id"] for item in result["items"]]
        assert ids == ["shared_skill", "extra_skill"]
        assert result["total"] == 2
        assert result["installable"] == 2
        # A member selected by either list starts selected.
        selected = {item["id"]: item["default_selected"] for item in result["items"]}
        assert selected["shared_skill"] is True

    def test_reports_already_installed_members_as_no_work(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles
        from tests.marketplace_fixtures import make_installed_row

        agent, package = _agent_entity(recommends=[{"type": "skill", "id": "demo_skill"}])
        skill, skill_package = _skill_entity()
        base_url, _root = _serve(
            tmp_path, serve_catalog, [(agent, package), (skill, skill_package)]
        )
        make_installed_row("demo_skill", type_="skill", version="1.0.0")

        transport = _source(base_url)
        manifest = transport.fetch_entity("agent", "demo_agent", "1.0.0")
        result = bundles.plan(manifest, transport)

        assert result["items"][0]["state"] == "installed"
        assert result["installable"] == 0
        assert result["selected"] == 0

    def test_probes_the_type_of_a_bare_member_id(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles

        agent, package = _agent_entity(bundle=["demo_skill"])
        skill, skill_package = _skill_entity()
        base_url, _root = _serve(
            tmp_path, serve_catalog, [(agent, package), (skill, skill_package)]
        )

        transport = _source(base_url)
        manifest = transport.fetch_entity("agent", "demo_agent", "1.0.0")
        result = bundles.plan(manifest, transport)

        assert result["items"][0]["type"] == "skill"
        assert result["items"][0]["name"] == "Demo Skill"

    def test_marks_types_that_cannot_be_installed_yet(
        self, marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
    ):
        from core.marketplace import bundles, installer

        # Every shipped type is installable, so the unsupported branch is
        # exercised by narrowing the supported set: the planner must mark a
        # member the dispatcher cannot reach instead of offering Install.
        monkeypatch.setattr(installer, "SUPPORTED_TYPES", ("skill", "agent"))

        agent, package = _agent_entity(bundle=[
            {"type": "rag", "id": "demo_rag"},
            {"type": "skill", "id": "demo_skill"},
        ])
        skill, skill_package = _skill_entity()
        rag, rag_package = make_entity(
            entity_id="demo_rag", version="1.0.0", type_="rag",
            files={"entity.json": "{}\n"},
        )
        base_url, _root = _serve(
            tmp_path,
            serve_catalog,
            [(agent, package), (skill, skill_package), (rag, rag_package)],
        )

        transport = _source(base_url)
        manifest = transport.fetch_entity("agent", "demo_agent", "1.0.0")
        result = bundles.plan(manifest, transport)

        states = {item["id"]: item["state"] for item in result["items"]}
        assert states["demo_rag"] == "unsupported"
        assert states["demo_skill"] == "install"
        assert result["installable"] == 1

    def test_every_shipped_type_is_installable(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles

        agent, package = _agent_entity(bundle=[
            {"type": "rag", "id": "demo_rag"},
            {"type": "mcp", "id": "demo_mcp"},
            {"type": "skill", "id": "demo_skill"},
        ])
        skill, skill_package = _skill_entity()
        rag, rag_package = make_entity(
            entity_id="demo_rag", version="1.0.0", type_="rag",
            files={"entity.json": "{}\n"},
        )
        mcp, mcp_package = make_entity(
            entity_id="demo_mcp", version="1.0.0", type_="mcp", files={},
            extra={"preset": {"command": "demo", "args": [], "env": {}},
                   "preset_id": "demo_mcp"},
        )
        base_url, _root = _serve(
            tmp_path,
            serve_catalog,
            [(agent, package), (skill, skill_package), (rag, rag_package),
             (mcp, mcp_package)],
        )

        transport = _source(base_url)
        manifest = transport.fetch_entity("agent", "demo_agent", "1.0.0")
        result = bundles.plan(manifest, transport)

        states = {item["id"]: item["state"] for item in result["items"]}
        assert states["demo_rag"] == "install"
        assert states["demo_mcp"] == "install"
        assert states["demo_skill"] == "install"
        assert result["installable"] == 3

    def test_missing_member_does_not_blank_the_preview(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles

        agent, package = _agent_entity(bundle=[
            {"type": "skill", "id": "ghost_skill"},
            {"type": "skill", "id": "demo_skill"},
        ])
        skill, skill_package = _skill_entity()
        base_url, _root = _serve(
            tmp_path, serve_catalog, [(agent, package), (skill, skill_package)]
        )

        transport = _source(base_url)
        manifest = transport.fetch_entity("agent", "demo_agent", "1.0.0")
        result = bundles.plan(manifest, transport)

        states = {item["id"]: item["state"] for item in result["items"]}
        assert states["ghost_skill"] == "unavailable"
        assert states["demo_skill"] == "install"
        assert result["installable"] == 1


class TestBundleInstall:
    def test_installs_every_selected_member_and_stamps_the_bundle(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles, registry

        agent, package = _agent_entity(bundle=[
            {"type": "skill", "id": "demo_skill", "default_selected": True},
        ])
        skill, skill_package = _skill_entity()
        base_url, _root = _serve(
            tmp_path, serve_catalog, [(agent, package), (skill, skill_package)]
        )
        transport = _source(base_url)
        manifest = transport.fetch_entity("agent", "demo_agent", "1.0.0")

        report = bundles.install(manifest, channel="main", source=transport)

        assert report["ok"] is True
        assert report["noop"] is False
        installed = {entry["id"] for entry in report["installed"]}
        assert installed == {"demo_skill"}
        row = registry.get_installed("skill", "demo_skill")
        assert row["bundle_id"] == "demo_agent"
        run = registry.get_installed("skill", "demo_skill")
        assert run["install_id"]

    def test_unselected_member_is_left_alone(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles, registry

        agent, package = _agent_entity(bundle=[
            {"type": "skill", "id": "demo_skill", "default_selected": False},
        ])
        skill, skill_package = _skill_entity()
        base_url, _root = _serve(
            tmp_path, serve_catalog, [(agent, package), (skill, skill_package)]
        )
        transport = _source(base_url)
        manifest = transport.fetch_entity("agent", "demo_agent", "1.0.0")

        report = bundles.install(manifest, channel="main", source=transport)

        assert report["noop"] is True
        assert registry.get_installed("skill", "demo_skill") is None

    def test_explicit_selection_overrides_the_defaults(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles, registry

        agent, package = _agent_entity(bundle=[
            {"type": "skill", "id": "first_skill", "default_selected": False},
            {"type": "skill", "id": "second_skill", "default_selected": True},
        ])
        first, first_package = _skill_entity("first_skill")
        second, second_package = _skill_entity("second_skill")
        base_url, _root = _serve(
            tmp_path,
            serve_catalog,
            [(agent, package), (first, first_package), (second, second_package)],
        )
        transport = _source(base_url)
        manifest = transport.fetch_entity("agent", "demo_agent", "1.0.0")

        report = bundles.install(
            manifest,
            channel="main",
            source=transport,
            selected=[{"type": "skill", "id": "first_skill"}],
        )

        installed = {entry["id"] for entry in report["installed"]}
        assert installed == {"first_skill"}
        assert registry.get_installed("skill", "second_skill") is None

    def test_already_installed_member_is_not_reported_as_new(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles
        from tests.marketplace_fixtures import make_installed_row

        agent, package = _agent_entity(bundle=[{"type": "skill", "id": "demo_skill"}])
        skill, skill_package = _skill_entity()
        base_url, _root = _serve(
            tmp_path, serve_catalog, [(agent, package), (skill, skill_package)]
        )
        make_installed_row("demo_skill", type_="skill", version="1.0.0")
        transport = _source(base_url)
        manifest = transport.fetch_entity("agent", "demo_agent", "1.0.0")

        report = bundles.install(manifest, channel="main", source=transport)

        assert report["noop"] is True
        assert report["installed"] == []

    def test_failure_rolls_back_the_members_this_run_installed(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles, registry
        from core.marketplace.bundles import BundleError

        # A healthy member goes in first, then a member whose manifest hash
        # no package can match fails: everything this run installed must be
        # undone, not only the member that failed.
        agent, package = _agent_entity(bundle=[
            {"type": "skill", "id": "good_skill"},
            {"type": "skill", "id": "broken_skill"},
        ])
        good, good_package = _skill_entity("good_skill")
        broken, broken_package = _skill_entity("broken_skill")
        broken = dict(broken)
        broken["files"] = dict(broken["files"])
        broken["files"]["SKILL.md"] = "0" * 64
        base_url, _root = _serve(
            tmp_path,
            serve_catalog,
            [(agent, package), (good, good_package), (broken, broken_package)],
        )
        transport = _source(base_url)
        manifest = transport.fetch_entity("agent", "demo_agent", "1.0.0")

        with pytest.raises(BundleError) as caught:
            bundles.install(manifest, channel="main", source=transport)

        assert caught.value.code == "bundle_install_failed"
        assert registry.get_installed("skill", "good_skill") is None
        assert registry.get_installed("skill", "broken_skill") is None
        assert registry.runtime_folder("skill", "good_skill") is None
        rolled = caught.value.details.get("rolled_back") or []
        assert [entry["id"] for entry in rolled] == ["good_skill"]
        assert rolled[0]["ok"] is True

    def test_rollback_group_undoes_the_whole_set(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles, registry
        from core.orchestrator_folders import orchestrator_folder_exists

        # One carrier recommends a skill and a second agent, so the group
        # holds two entities of different types.
        companion, companion_package = _agent_entity(entity_id="companion_agent")
        skill, skill_package = _skill_entity()
        carrier, carrier_package = _agent_entity(
            entity_id="carrier_agent",
            bundle=[
                {"type": "skill", "id": "demo_skill"},
                {"type": "agent", "id": "companion_agent"},
            ],
        )
        base_url, _root = _serve(
            tmp_path,
            serve_catalog,
            [(carrier, carrier_package), (companion, companion_package),
             (skill, skill_package)],
        )
        transport = _source(base_url)
        manifest = transport.fetch_entity("agent", "carrier_agent", "1.0.0")

        installed = bundles.install(manifest, channel="main", source=transport)

        group_id = installed["group_id"]
        assert group_id
        assert {entry["id"] for entry in installed["installed"]} == {
            "demo_skill", "companion_agent"
        }
        assert orchestrator_folder_exists("companion_agent") is True

        result = bundles.rollback_group(group_id)

        assert result["ok"] is True
        assert [entry["id"] for entry in result["members"]] == [
            "companion_agent", "demo_skill"
        ]
        assert all(entry["ok"] for entry in result["members"])
        assert registry.get_installed("skill", "demo_skill") is None
        assert registry.get_installed("agent", "companion_agent") is None
        assert orchestrator_folder_exists("companion_agent") is False


class TestCatalogBundles:
    def test_index_bundles_are_listed(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles

        agent, package = _agent_entity()
        base_url, _root = _serve(
            tmp_path,
            serve_catalog,
            [(agent, package)],
            bundles=[{"id": "starter", "name": "Starter Set", "items": []}],
        )

        listed = bundles.catalog_bundles(_source(base_url))

        assert [entry["id"] for entry in listed] == ["starter"]
        assert listed[0]["name"] == "Starter Set"

    def test_catalog_without_bundles_lists_nothing(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles

        agent, package = _agent_entity()
        base_url, _root = _serve(tmp_path, serve_catalog, [(agent, package)])

        assert bundles.catalog_bundles(_source(base_url)) == []

    def test_bundle_plan_resolves_its_members(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles

        agent, package = _agent_entity()
        skill, skill_package = _skill_entity()
        base_url, root = _serve(
            tmp_path,
            serve_catalog,
            [(agent, package), (skill, skill_package)],
            bundles=[{
                "id": "starter",
                "name": "Starter Set",
                "version": "1.0.0",
                "items": [{"type": "skill", "id": "demo_skill", "version": "1.0.0"}],
            }],
        )

        result = bundles.plan_bundle("starter", _source(base_url))

        assert result["bundle"]["id"] == "starter"
        assert [item["id"] for item in result["items"]] == ["demo_skill"]
        assert result["installable"] == 1

    def test_empty_bundle_is_refused(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles

        agent, package = _agent_entity()
        base_url, _root = _serve(
            tmp_path,
            serve_catalog,
            [(agent, package)],
            bundles=[{"id": "hollow", "name": "Hollow", "items": []}],
        )

        with pytest.raises(bundles.BundleError) as caught:
            bundles.load_bundle("hollow", _source(base_url))
        assert caught.value.code == "empty_bundle"

    def test_bundle_is_cached_for_offline_use(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import bundles, store
        from core.marketplace.source import MarketplaceSource

        agent, package = _agent_entity()
        base_url, _root = _serve(
            tmp_path,
            serve_catalog,
            [(agent, package)],
            bundles=[{"id": "starter", "name": "Starter Set", "items": ["demo_agent"]}],
        )
        bundles.load_bundle("starter", _source(base_url))

        cached = store.read_json(
            Path(store.manifest_cache_dir(), "bundle__starter.json"), default=None
        )
        assert isinstance(cached, dict)
        assert cached["id"] == "starter"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
