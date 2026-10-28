"""
The ``marketplace.enabled`` master switch (P3, D-21).

One switch has to cover three places, not one: the sidebar entry, the
one-time seeding of registry rows and the default-orchestrator import in
``core.orchestrators``. Each of them is checked here against a disabled
switch, because a flag that is honoured in two places out of three is
worse than no flag - the section disappears while its side effects stay.
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
PKG_ROOT = HERE.parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from tests._st_mock import StopRerun, install_streamlit_mock  # noqa: E402


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


def _fresh_ui():
    """Import ``ui.app`` fresh under the active Streamlit mock."""
    for name in list(sys.modules):
        if name.startswith(("core", "storage", "ui")):
            sys.modules.pop(name, None)
    return importlib.import_module("ui.app")


def _state():
    """Return the session state a rendered main() expects."""
    return dict(
        show_skill_form=False,
        edit_skill_id=None,
        user_input_value="",
        force_send=False,
        active_thread_id=None,
        confirm_delete_all=False,
        attached_file_context="",
        attached_file_name="",
        input_key=0,
        current_page="run",
        ui_lang="English",
    )


def _render_sidebar(monkeypatch, enabled: bool, registry_patch=None):
    """Render ``main()`` with the given switch state and return the mock.

    The patches are applied AFTER the fresh import: dropping the project
    modules re-imports ``core.marketplace.config``, so a patch made before
    the import would land on the discarded module object and change
    nothing.
    """
    os.environ.setdefault("SAGAAI_DATA_DIR", tempfile.mkdtemp())
    with install_streamlit_mock() as mock:
        ui_app = _fresh_ui()
        _settings(monkeypatch, enabled=enabled)
        if registry_patch is not None:
            from core.marketplace import registry

            monkeypatch.setattr(registry, "seed_local_orphans", registry_patch)
        mock.session_state.update(_state())
        try:
            ui_app.main()
        except StopRerun:
            pass
    return mock


def _nav_keys(mock) -> list:
    """Return the ``key=`` of every rendered sidebar button."""
    return [call[2].get("key") for call in mock.calls if call[0] == "button"]


def _has_marketplace_nav(mock) -> bool:
    """True when the marketplace block rendered at least one subsection."""
    return any(
        str(key or "").startswith("nav_marketplace")
        for key in _nav_keys(mock)
    )


# ─── Sidebar ──────────────────────────────────────────────────────────────

def test_the_sidebar_hides_the_section_when_the_switch_is_off(
    isolated_app_modules, monkeypatch
):
    mock = _render_sidebar(monkeypatch, enabled=False)

    assert not _has_marketplace_nav(mock)


def test_the_sidebar_shows_the_section_when_the_switch_is_on(
    isolated_app_modules, monkeypatch
):
    mock = _render_sidebar(monkeypatch, enabled=True)

    assert _has_marketplace_nav(mock)


def test_a_disabled_switch_does_not_break_the_rest_of_the_sidebar(
    isolated_app_modules, monkeypatch
):
    """The neighbouring entries stay: only the marketplace entry goes away."""
    mock = _render_sidebar(monkeypatch, enabled=False)
    keys = _nav_keys(mock)

    assert "nav_skills_library" in keys
    assert "nav_storage" in keys
    assert "nav_updates" in keys


# ─── Seeding ──────────────────────────────────────────────────────────────

def test_seeding_does_not_run_with_the_switch_off(
    isolated_app_modules, monkeypatch
):
    """A disabled section must not write registry rows during start-up."""
    called = []

    _render_sidebar(monkeypatch, enabled=False, registry_patch=lambda: called.append(True) or [])

    assert called == []


def test_seeding_runs_on_the_first_render_with_the_switch_on(
    isolated_app_modules, monkeypatch
):
    called = []

    _render_sidebar(monkeypatch, enabled=True, registry_patch=lambda: called.append(True) or [])

    assert called == [True]


def test_a_failing_seed_does_not_stop_the_application(
    isolated_app_modules, monkeypatch
):
    """A broken registry must never block the start of the UI."""
    def _broken():
        raise RuntimeError("registry unavailable")

    mock = _render_sidebar(monkeypatch, enabled=True, registry_patch=_broken)

    assert mock.errors == []
    assert _has_marketplace_nav(mock)


# ─── Default orchestrators ────────────────────────────────────────────────

def test_the_default_orchestrator_import_still_runs_with_the_switch_off(
    isolated_app_modules, monkeypatch
):
    """The switch gates the marketplace, not the bundled defaults."""
    _settings(monkeypatch, enabled=False)
    from core import default_imports, orchestrators

    called = []
    monkeypatch.setattr(
        default_imports,
        "ensure_default_orchestrators",
        lambda: called.append(True) or {},
    )
    exported = []
    from core.marketplace import registry as marketplace_registry

    monkeypatch.setattr(
        marketplace_registry,
        "export_registry_cache",
        lambda: exported.append(True),
    )

    result = orchestrators._ensure_default_orchestrators()

    assert called == [True]
    assert result == {}
    # With the switch off the marketplace does not even refresh its cache.
    assert exported == []


def test_the_orchestrator_import_refreshes_the_cache_with_the_switch_on(
    isolated_app_modules, monkeypatch
):
    _settings(monkeypatch, enabled=True)
    from core import default_imports, orchestrators
    from core.marketplace import registry as marketplace_registry

    monkeypatch.setattr(
        default_imports,
        "ensure_default_orchestrators",
        lambda: {"dev_agent": "exists"},
    )
    exported = []
    monkeypatch.setattr(
        marketplace_registry,
        "export_registry_cache",
        lambda: exported.append(True) or "cache.json",
    )

    result = orchestrators._ensure_default_orchestrators()

    assert result == {"dev_agent": "exists"}
    assert exported == [True]


def test_a_broken_cache_export_does_not_fail_the_import(
    isolated_app_modules, monkeypatch
):
    _settings(monkeypatch, enabled=True)
    from core import default_imports, orchestrators
    from core.marketplace import registry as marketplace_registry

    monkeypatch.setattr(
        default_imports, "ensure_default_orchestrators", lambda: {"dev_agent": "exists"}
    )

    def _broken():
        raise OSError("disk full")

    monkeypatch.setattr(marketplace_registry, "export_registry_cache", _broken)

    assert orchestrators._ensure_default_orchestrators() == {"dev_agent": "exists"}
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
