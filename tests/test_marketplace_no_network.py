"""
The marketplace must not touch the network on start-up (acceptance #7).

Two independent checks, because they catch different mistakes. The static
one reads the source of the three start-up paths and refuses any call that
could reach the catalog - a call added there would be invisible at runtime
as long as the machine happened to be online. The runtime one bans
``socket.socket`` and renders the start-up sequence plus the Updates page,
which catches an import-time fetch the AST walk cannot see.

The showcase page is deliberately NOT rendered here: opening the catalog
is a user action and does fetch the index. What must never happen is a
fetch while the application boots.
"""
import ast
import socket
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
PKG_ROOT = HERE.parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from tests._st_mock import StopRerun, install_streamlit_mock  # noqa: E402
from tests.marketplace_fixtures import marketplace_data_dir  # noqa: E402,F401 - fixture


#: Attribute names that would reach the catalog.
NETWORK_CALLS = frozenset({
    "fetch_index",
    "fetch_entity",
    "fetch_package",
    "fetch_bytes",
    "check_entity_updates",
    "apply_entity_updates",
    "urlopen",
})


# ─── Static check ─────────────────────────────────────────────────────────

def _called_names(path: Path) -> set:
    """Return every function/attribute name called anywhere in *path*.

    Both ``obj.name(...)`` and a bare ``name(...)`` are collected, so a
    module-level helper that wraps the transport is caught as well.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


@pytest.mark.parametrize(
    "relative",
    ["app.py", "core/default_imports.py", "core/marketplace/registry.py",
     "core/marketplace/bootstrap.py"],
)
def test_start_up_code_contains_no_catalog_calls(relative):
    """No start-up module may call the catalog transport."""
    called = _called_names(PKG_ROOT / relative)

    assert called & NETWORK_CALLS == set()


def test_the_check_function_is_not_referenced_by_the_start_up_path():
    """``check_entity_updates`` belongs to a button, not to the boot."""
    for relative in ("core/default_imports.py", "core/marketplace/bootstrap.py",
                     "core/marketplace/registry.py"):
        text = (PKG_ROOT / relative).read_text(encoding="utf-8")
        assert "check_entity_updates" not in text


def test_the_ui_app_does_not_call_the_catalog_either():
    """The sidebar render runs on every rerun - it must stay offline."""
    called = _called_names(PKG_ROOT / "ui" / "app.py")

    assert called & NETWORK_CALLS == set()


# ─── Runtime check ────────────────────────────────────────────────────────

class _SocketBan:
    """Replacement for ``socket.socket`` that records and refuses use."""

    attempts = []

    def __init__(self, *args, **kwargs):
        _SocketBan.attempts.append((args, kwargs))
        raise AssertionError("a socket was opened during start-up")


@pytest.fixture()
def socket_ban(monkeypatch):
    """Refuse any socket creation for the duration of the test."""
    _SocketBan.attempts = []
    monkeypatch.setattr(socket, "socket", _SocketBan)
    yield _SocketBan
    assert _SocketBan.attempts == []


def _settings(monkeypatch, **overrides):
    """Patch the marketplace settings for one test."""
    from core.marketplace import config as marketplace_config

    values = {
        "enabled": True,
        "base_url": "http://127.0.0.1:1",
        "allow_code": True,
        "require_signature": False,
        "channel": "main",
    }
    values.update(overrides)
    monkeypatch.setattr(marketplace_config, "marketplace_settings", lambda: dict(values))
    return values


def test_bootstrap_and_seeding_run_without_a_socket(
    marketplace_data_dir, socket_ban, monkeypatch  # noqa: F811 - imported fixture
):
    """The start-up sequence stays local even with a configured catalog."""
    _settings(monkeypatch)
    from core.marketplace import bootstrap

    summary = bootstrap.bootstrap()

    assert summary["enabled"] is True
    assert summary["ok"] is True
    assert socket_ban.attempts == []


def test_ensure_all_defaults_runs_without_a_socket(
    marketplace_data_dir, socket_ban, monkeypatch
):
    """Bundled defaults are local folders; nothing is downloaded."""
    _settings(monkeypatch)
    from core.default_imports import ensure_all_defaults

    ensure_all_defaults()

    assert socket_ban.attempts == []


def test_the_updates_page_renders_without_a_socket(
    marketplace_data_dir, socket_ban, monkeypatch
):
    """Opening Updates must not check the catalog until asked."""
    _settings(monkeypatch)
    with install_streamlit_mock() as mock:
        mock.session_state["ui_lang"] = "en"
        for name in list(sys.modules):
            if name.startswith("ui.pages.updates"):
                sys.modules.pop(name, None)
        from ui.pages.updates import page_updates

        try:
            page_updates()
        except StopRerun:
            pass

    assert socket_ban.attempts == []
    assert mock.errors == []
    labels = [args[0] for name, args, _kw in mock.calls if name == "button"]
    assert labels, "the Updates page rendered no buttons"


def test_the_marketplace_sections_show_a_prompt_instead_of_fetching(
    marketplace_data_dir, socket_ban, monkeypatch
):
    _settings(monkeypatch)
    with install_streamlit_mock() as mock:
        mock.session_state["ui_lang"] = "en"
        for name in list(sys.modules):
            if name.startswith("ui.pages.updates_marketplace"):
                sys.modules.pop(name, None)
        from ui.pages.updates_marketplace import render_marketplace_sections

        render_marketplace_sections("en")

    assert socket_ban.attempts == []
    captions = [args[0] for name, args, _kw in mock.calls if name == "caption"]
    assert any("Press the button" in str(text) for text in captions)


def test_the_showcase_card_is_the_only_place_that_may_fetch(
    marketplace_data_dir, monkeypatch  # noqa: F811 - imported fixture
):
    """The catalog read is reachable from the showcase, not from start-up."""
    page = (PKG_ROOT / "ui" / "pages" / "marketplace.py").read_text(encoding="utf-8")

    assert "list_entities" in page
    boot = (PKG_ROOT / "ui" / "app.py").read_text(encoding="utf-8")
    assert "list_entities" not in boot
    assert "list_skills" not in boot
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
