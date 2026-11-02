# -*- coding: utf-8 -*-
"""
Tests for the one-shot notice shown after a legacy devagent.db is archived.

The notice is the only thing that tells a user their dialogue list became
empty on purpose, so the tests pin both halves: it appears exactly once and it
names the archive path.
"""
import sys
import types
from unittest.mock import MagicMock

import pytest


class _StopRerun(Exception):
    """Raised by the Streamlit stub to end a render pass like st.rerun()."""


@pytest.fixture
def st_env(monkeypatch):
    """Minimal Streamlit stub: session state, warnings and rerun.

    Only the surface ``ui.app`` touches is provided; every attribute access
    that the module does not need is deliberate noise, so an unexpected call
    fails the test instead of passing silently.
    """
    st = types.ModuleType("streamlit")
    state = {}

    class _SessionState(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    st.session_state = _SessionState(state)
    st.warnings = []
    st.successes = []

    def _warning(text, *a, **kw):
        st.warnings.append(text)

    def _success(text, *a, **kw):
        st.successes.append(text)

    st.warning = _warning
    st.success = _success
    st.info = lambda *a, **kw: None
    st.error = lambda *a, **kw: None
    st.rerun = lambda *a, **kw: (_ for _ in ()).throw(_StopRerun())
    st.set_page_config = lambda *a, **kw: None
    st.markdown = lambda *a, **kw: None
    st.title = lambda *a, **kw: None
    st.caption = lambda *a, **kw: None
    st.button = lambda *a, **kw: False
    st.expander = lambda *a, **kw: MagicMock()
    st.selectbox = lambda label, options, **kw: (options[0] if options else "")
    st.sidebar = MagicMock()
    st.sidebar.__enter__ = lambda self: self
    st.sidebar.__exit__ = lambda self, *a: False
    monkeypatch.setitem(sys.modules, "streamlit", st)
    return st


def _app_module(st_env, monkeypatch):
    """Import ui.app against the stub and neutralise unrelated start-up work.

    ``ui.components.flash`` is dropped from the cache too: it binds
    ``streamlit`` at import time, so a copy left over from an earlier test
    would queue messages into that test's stub instead of this one.
    """
    for name in ("ui.app", "ui.components.flash"):
        sys.modules.pop(name, None)
    import ui.app as app_mod

    monkeypatch.setattr(app_mod, "require_auth", lambda: None, raising=False)
    monkeypatch.setattr(app_mod, "load_env_from_shell_profiles", lambda: None, raising=False)
    monkeypatch.setattr(app_mod, "_run_dependency_check_once", lambda: None, raising=False)
    monkeypatch.setattr(app_mod, "_restore_ui_reload_state", lambda: None, raising=False)
    monkeypatch.setattr(app_mod, "_handle_thread_deeplink", lambda: None, raising=False)
    monkeypatch.setattr(app_mod, "_sync_saved_theme_once", lambda cfg: None, raising=False)
    monkeypatch.setattr(app_mod, "_apply_theme", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(app_mod, "_build_orch_nav", lambda: ([], []), raising=False)
    monkeypatch.setattr(app_mod, "_build_assistants_nav", lambda lang: ([], []), raising=False)
    monkeypatch.setattr(app_mod.default_imports, "ensure_all_defaults", lambda: None, raising=False)
    monkeypatch.setattr(app_mod, "migrate_platform_workspace_meta", lambda: 0, raising=False)
    monkeypatch.setattr(app_mod, "marketplace_enabled", lambda: False, raising=False)
    monkeypatch.setattr(app_mod, "list_orchestrators", lambda: [], raising=False)
    monkeypatch.setattr(app_mod, "page_welcome", lambda: None, raising=False)
    return app_mod


# ── The notice ───────────────────────────────────────────────────────────────


def test_the_notice_names_the_archive_and_the_preserved_dialogues(st_env, tmp_path, monkeypatch):
    """The message has to say where the old dialogues went."""
    from core import devagent_db_archive as A

    A._write_notice({"archive_path": "/data/devagent.db.zip", "threads": 305,
                     "messages": 70164}, str(tmp_path))
    app_mod = _app_module(st_env, monkeypatch)
    monkeypatch.setattr(A, "_notice_path", lambda data_dir=None: str(tmp_path / A.NOTICE_FILE_NAME))

    app_mod._show_db_archive_notice_once("English")
    app_mod.render_flash()

    assert len(st_env.warnings) == 1
    text = st_env.warnings[0]
    assert "/data/devagent.db.zip" in text
    assert "305" in text
    assert "70164" in text


def test_the_notice_is_shown_only_once_per_session(st_env, tmp_path, monkeypatch):
    """A rerun must not repeat a finished migration message."""
    from core import devagent_db_archive as A

    A._write_notice({"archive_path": "/data/x.zip", "threads": 1,
                     "messages": 2}, str(tmp_path))
    app_mod = _app_module(st_env, monkeypatch)
    monkeypatch.setattr(A, "_notice_path", lambda data_dir=None: str(tmp_path / A.NOTICE_FILE_NAME))

    app_mod._show_db_archive_notice_once("English")
    app_mod._show_db_archive_notice_once("English")
    app_mod.render_flash()

    assert len(st_env.warnings) == 1


def test_without_a_notice_nothing_is_shown(st_env, tmp_path, monkeypatch):
    """An installation that never had a legacy database stays silent."""
    from core import devagent_db_archive as A

    app_mod = _app_module(st_env, monkeypatch)
    monkeypatch.setattr(A, "_notice_path", lambda data_dir=None: str(tmp_path / A.NOTICE_FILE_NAME))

    app_mod._show_db_archive_notice_once("English")
    app_mod.render_flash()

    assert st_env.warnings == []


def test_a_broken_notice_reader_does_not_break_the_page(st_env, monkeypatch):
    """Start-up must survive a damaged notice file."""
    from core import devagent_db_archive as A

    app_mod = _app_module(st_env, monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("notice unreadable")

    monkeypatch.setattr(A, "take_notice", _boom)

    app_mod._show_db_archive_notice_once("English")  # must not raise
    app_mod.render_flash()

    assert st_env.warnings == []


def test_the_message_is_translated_in_all_shipped_languages():
    """A missing key would surface as a raw identifier in the UI."""
    from core.i18n import t

    for lang in ("Русский", "English"):
        text = t("devagent_db_archived", lang=lang, path="/data/a.zip",
                 threads=3, messages=7)
        assert "devagent_db_archived" != text
        assert "/data/a.zip" in text
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
