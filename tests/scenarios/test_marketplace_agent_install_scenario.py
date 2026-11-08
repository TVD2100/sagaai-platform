# -*- coding: utf-8 -*-
"""
Installation scenario - an agent from the catalog, end to end.

Walks the path a user takes: install an agent whose package names no model
and repeats the catalog's English name, then open its chat page and read
the name the interface shows. The assertions are on the installed record and
on the gate answer, because a page that opens an employee which cannot run,
or shows a foreign name, is the failure mode that matters here.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)

PACKAGE_NAME = "Project Manager"
CARD_NAMES = {"en": "Project Manager", "ru": "Менеджер проектов", "zh-CN": "项目经理"}


def _agent_files(slug="pm_agent", name=PACKAGE_NAME, config=None):
    """Return a package that names its own settings but no model."""
    bundle = {
        "format": "sagaai_orchestrator/v1",
        "slug": slug,
        "name": name,
        "description": "An agent used by the installation scenario",
        "config": config if config is not None else {
            "max_steps": 40,
            "chat_web_search": False,
            "memory_dir": "pm_memory",
        },
        "tools": [],
        "max_steps": 40,
    }
    return {
        "orchestrator.json": json.dumps(bundle, ensure_ascii=False, indent=2),
        "system_prompt.md": "You are a project manager.\n",
    }


def _install(tmp_path, serve_catalog, *, files=None, names=None, config_defaults=None,
             entity_id="pm_agent", version="1.0.0"):
    """Serve a one-agent catalog and install it through the real installer.

    Returns ``(entity, report)`` so a test can assert on both sides.
    """
    from core.marketplace.agent_installer import install_agent
    from core.marketplace.source import MarketplaceSource

    extra = {}
    if names is not None:
        extra["name"] = names
    if config_defaults is not None:
        extra["config_defaults"] = config_defaults
    entity, package = make_entity(
        entity_id=entity_id,
        version=version,
        type_="agent",
        files=files if files is not None else _agent_files(entity_id),
        extra=extra or None,
    )
    entity.setdefault("agent_kind", "orchestrator")
    root = make_catalog(tmp_path / f"catalog_{version}", [(entity, package)])
    base_url = serve_catalog(root)
    report = install_agent(entity, channel="main", source=MarketplaceSource(base_url))
    return entity, report


def _gate_opens(slug):
    """Return whether the chat page would open for *slug*.

    Runs the real header helper with a stubbed page surface and a key store
    that answers "yes" for every provider: the question under test is which
    provider the page asks about at all.
    """
    import ui.pages.orchestrator_chat as chat_page
    from core.orchestrators import get_orchestrator

    asked = []
    warnings = []

    class _Stub:
        @staticmethod
        def caption(*args, **kwargs):
            pass

        @staticmethod
        def markdown(*args, **kwargs):
            pass

        @staticmethod
        def warning(*args, **kwargs):
            warnings.append(args)

    original_st, original_t = chat_page.st, chat_page.t
    original_key = chat_page._assistant_has_api_key
    chat_page.st = _Stub
    chat_page.t = lambda key, **kwargs: key
    chat_page._assistant_has_api_key = lambda service: (asked.append(service), True)[-1]
    try:
        shown = chat_page._render_chat_header(
            get_orchestrator(slug), "Name", "Russian"
        )
    finally:
        chat_page.st, chat_page.t = original_st, original_t
        chat_page._assistant_has_api_key = original_key
    assert warnings == []
    return shown, asked


def test_an_installed_agent_opens_its_chat_page(
    marketplace_data_dir, serve_catalog, tmp_path
):
    """The reported defect: the package names no model, so the page blocked."""
    from core.orchestrators import get_orchestrator

    _install(tmp_path, serve_catalog)

    config = get_orchestrator("pm_agent")["config"]
    assert config["strong_service"] == "DeepSeek"
    assert config["strong_model"]
    # The package's own settings survive the platform basis.
    assert config["max_steps"] == 40
    assert config["memory_dir"] == "pm_memory"

    shown, asked = _gate_opens("pm_agent")
    assert shown is True
    assert asked == ["DeepSeek"]


def test_the_interface_shows_the_name_in_its_own_language(
    marketplace_data_dir, serve_catalog, tmp_path
):
    """The reported defect: an English name in a Russian interface."""
    from core.naming import display_name
    from core.orchestrators import get_orchestrator

    _install(tmp_path, serve_catalog, names=CARD_NAMES)

    row = get_orchestrator("pm_agent")
    assert display_name(row, "ru") == "Менеджер проектов"
    assert display_name(row, "en") == "Project Manager"
    assert display_name(row, "zh") == "项目经理"


def test_an_author_own_name_is_kept_as_given(
    marketplace_data_dir, serve_catalog, tmp_path
):
    """A package name the card does not repeat is the author's own wording."""
    from core.naming import display_name
    from core.orchestrators import get_orchestrator

    _install(
        tmp_path,
        serve_catalog,
        files=_agent_files(name="Мой личный помощник"),
        names=CARD_NAMES,
    )

    row = get_orchestrator("pm_agent")
    assert row["name_user"] == "Мой личный помощник"
    # A user-defined name shows in every language - that is the documented
    # rule for an entity whose owner typed one.
    assert display_name(row, "ru") == "Мой личный помощник"
    assert display_name(row, "en") == "Мой личный помощник"


def test_the_catalog_defaults_still_fill_gaps_after_the_platform_basis(
    marketplace_data_dir, serve_catalog, tmp_path
):
    """Order matters: the catalog defaults, the basis and the package all meet."""
    from core.orchestrators import get_orchestrator

    _install(
        tmp_path,
        serve_catalog,
        config_defaults={"reasoning_effort": "high", "strong_model": "from_manifest"},
    )

    config = get_orchestrator("pm_agent")["config"]
    assert config["reasoning_effort"] == "high"
    # The platform basis runs last but never overwrites an earlier value.
    assert config["strong_model"] == "from_manifest"


def test_opening_the_page_twice_does_not_change_the_agent(
    marketplace_data_dir, serve_catalog, tmp_path
):
    """Reading the page is not a write: a second open sees the same agent."""
    from core.orchestrators import get_orchestrator

    _install(tmp_path, serve_catalog, names=CARD_NAMES)
    before = dict(get_orchestrator("pm_agent"))

    assert _gate_opens("pm_agent")[0] is True
    assert _gate_opens("pm_agent")[0] is True

    after = get_orchestrator("pm_agent")
    assert after["config"] == before["config"]
    assert after["name_ru"] == "Менеджер проектов"


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
