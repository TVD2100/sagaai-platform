# -*- coding: utf-8 -*-
"""tests/test_ui_personal_memory.py - UI of the personal memory layer.

Three entry points are rendered under the Streamlit mock and asserted on
what they actually do, not merely that they run:

* the employee settings tab (working folder + own memory) - a save must reach
  ``save_orchestrator(memory_dir=...)`` / ``personal_memory.write_memory`` and
  must keep the user on the same tab;
* the connection card - notes are written through the personal-memory layer,
  so the credential screen of that layer applies to them;
* the settings page - the owner profile editor writes the ``user`` scope.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


def _invoke(fn):
    """Run a render function, treating st.rerun() as the end of the pass."""
    try:
        fn()
    except StopRerun:
        pass


@pytest.fixture()
def mock_env(tmp_path, monkeypatch):
    """Streamlit mock with real i18n and an isolated personal-memory root."""
    for name in list(sys.modules):
        if name == 'ui' or name.startswith('ui.'):
            sys.modules.pop(name, None)
    import core.paths
    monkeypatch.setattr(core.paths, 'DATA_DIR', str(tmp_path))
    with install_streamlit_mock() as st:
        st.session_state.update(ui_lang='English')
        yield st


ORCH = {
    'slug': 'memo_role',
    'name': 'Memo Role',
    'prompt_text': 'system prompt',
    'config': {},
}


# ─── Employee settings tab ───────────────────────────────────────────────────


def test_memory_tab_renders_folder_and_editor(mock_env):
    """The tab shows the stored folder, the resolved path and the memory body."""
    import ui.pages.orchestrator as orch_mod
    from core import personal_memory as pm

    scope = pm.format_scope(pm.SCOPE_ORCHESTRATOR, 'memo_role')
    pm.write_memory(scope, '## Notes\n- kept from a previous session')

    with patch.object(orch_mod, 'get_orchestrator', return_value=dict(ORCH)), \
         patch.object(orch_mod, '_thread_folder', return_value='/tmp/project'):
        _invoke(lambda: orch_mod._render_orch_memory('memo_role', 'English'))

    assert not mock_env.errors
    texts = {kwargs.get('key'): kwargs.get('value')
             for name, _args, kwargs in mock_env.calls if name == 'text_area'}
    assert 'kept from a previous session' in str(texts.get('orch_memory_text_memo_role'))
    folders = {kwargs.get('key'): kwargs.get('value')
               for name, _args, kwargs in mock_env.calls if name == 'text_input'}
    assert 'orch_memory_dir_memo_role' in folders


def test_memory_save_writes_the_scope_and_pins_the_tab(mock_env):
    """Saving the editor stores the body and keeps the active settings tab."""
    import ui.pages.orchestrator as orch_mod
    from core import personal_memory as pm

    scope = pm.format_scope(pm.SCOPE_ORCHESTRATOR, 'memo_role')
    mock_env._text_returns['orch_memory_text_memo_role'] = '## Rules\n- answer briefly'
    mock_env.click('orch_save_memory_memo_role')

    with patch.object(orch_mod, 'get_orchestrator', return_value=dict(ORCH)), \
         patch.object(orch_mod, '_thread_folder', return_value='/tmp/project'):
        _invoke(lambda: orch_mod._render_orch_memory('memo_role', 'English'))

    assert not mock_env.errors
    assert 'answer briefly' in pm.read_memory(scope)['body']
    assert pm.memory_meta(scope)['revision'] == 1
    assert mock_env.session_state.get('orch_settings_section_memo_role') == 'memory'


def test_memory_save_refuses_a_credential(mock_env):
    """A paste that looks like a token is rejected with a visible error."""
    import ui.pages.orchestrator as orch_mod
    from core import personal_memory as pm

    scope = pm.format_scope(pm.SCOPE_ORCHESTRATOR, 'memo_role')
    mock_env._text_returns['orch_memory_text_memo_role'] = (
        'token = ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345'
    )
    mock_env.click('orch_save_memory_memo_role')

    with patch.object(orch_mod, 'get_orchestrator', return_value=dict(ORCH)), \
         patch.object(orch_mod, '_thread_folder', return_value='/tmp/project'):
        _invoke(lambda: orch_mod._render_orch_memory('memo_role', 'English'))

    assert not pm.memory_exists(scope)
    queued = mock_env.session_state.get('_flash_messages') or []
    assert any(m.get('kind') == 'error' for m in queued)


def test_folder_save_goes_through_save_orchestrator(mock_env):
    """The folder button writes the config key, not a separate column."""
    import ui.pages.orchestrator as orch_mod

    mock_env._text_returns['orch_memory_dir_memo_role'] = 'bases/memo'
    mock_env.click('orch_save_memory_dir_memo_role')
    calls = []

    def fake_save(slug, **kwargs):
        calls.append((slug, kwargs))
        return True

    with patch.object(orch_mod, 'get_orchestrator', return_value=dict(ORCH)), \
         patch.object(orch_mod, 'save_orchestrator', side_effect=fake_save), \
         patch.object(orch_mod, '_thread_folder', return_value='/tmp/project'):
        _invoke(lambda: orch_mod._render_orch_memory('memo_role', 'English'))

    assert calls == [('memo_role', {'memory_dir': 'bases/memo'})]
    assert mock_env.session_state.get('orch_settings_section_memo_role') == 'memory'


def test_folder_failure_reports_and_stays(mock_env):
    """A failed folder save surfaces an error and does not move the tab."""
    import ui.pages.orchestrator as orch_mod

    mock_env.click('orch_save_memory_dir_memo_role')
    with patch.object(orch_mod, 'get_orchestrator', return_value=dict(ORCH)), \
         patch.object(orch_mod, 'save_orchestrator', return_value=False), \
         patch.object(orch_mod, '_thread_folder', return_value='/tmp/project'):
        _invoke(lambda: orch_mod._render_orch_memory('memo_role', 'English'))

    queued = mock_env.session_state.get('_flash_messages') or []
    assert any(m.get('kind') == 'error' for m in queued)
    assert mock_env.session_state.get('orch_settings_section_memo_role') == 'memory'


# ─── Connection card ─────────────────────────────────────────────────────────


def test_connection_card_saves_notes(mock_env):
    """The card writes the connector scope through the memory layer."""
    import ui.pages.connectors as conn_mod
    from core import personal_memory as pm

    conn = {'id': 'conn1', 'name': 'Server', 'service': 'ssh', 'account': 'u'}
    mock_env._text_returns['conn_memory_conn1'] = '## Notes\n- port 2222 is blocked'
    mock_env.click('conn_memory_save_conn1')

    with patch.object(pm, 'memory_path', wraps=pm.memory_path):
        with patch('core.connectors.set_connection_memory', return_value=True) as setter:
            _invoke(lambda: conn_mod._render_connection_card(conn, 'English'))

    setter.assert_called_once()
    args, _kwargs = setter.call_args
    assert args[0] == 'conn1'
    assert 'port 2222 is blocked' in args[1]
    queued = mock_env.session_state.get('_flash_messages') or []
    assert any(m.get('kind') == 'success' for m in queued)


def test_connection_card_shows_stored_notes(mock_env):
    """Existing notes are pre-loaded into the card's editor."""
    import ui.pages.connectors as conn_mod

    conn = {'id': 'conn1', 'name': 'Server', 'service': 'ssh'}
    with patch('core.connectors.get_connection_memory',
               return_value='## Notes\n- already known'):
        _invoke(lambda: conn_mod._render_connection_card(conn, 'English'))

    assert not mock_env.errors
    values = [kwargs.get('value') for name, _args, kwargs in mock_env.calls
              if name == 'text_area']
    assert any('already known' in str(v) for v in values)


def test_connection_card_refuses_a_credential(mock_env):
    """A secret pasted into the notes is refused and never stored."""
    import ui.pages.connectors as conn_mod

    conn = {'id': 'conn1', 'name': 'Server', 'service': 'ssh'}
    mock_env._text_returns['conn_memory_conn1'] = (
        '-----BEGIN RSA PRIVATE KEY-----\nMIIE'
    )
    mock_env.click('conn_memory_save_conn1')

    with patch('core.connectors.set_connection_memory', return_value=True) as setter:
        _invoke(lambda: conn_mod._render_connection_card(conn, 'English'))

    setter.assert_not_called()
    errors = [msg for name, (msg,), _kwargs in mock_env.calls if name == 'error']
    assert errors


# ─── Owner profile (settings page) ───────────────────────────────────────────


def test_owner_profile_editor_roundtrip(mock_env):
    """The profile editor loads and saves the ``user`` scope."""
    import ui.pages.settings as settings_mod
    from core import personal_memory as pm

    pm.write_memory(pm.SCOPE_USER, '## Rules\n- speak plainly')
    _invoke(lambda: settings_mod._render_personal_memory('English'))
    values = [kwargs.get('value') for name, _args, kwargs in mock_env.calls
              if name == 'text_area']
    assert any('speak plainly' in str(v) for v in values)

    mock_env._text_returns['settings_memory_text'] = '## Rules\n- be terse'
    mock_env.click('settings_memory_save')
    _invoke(lambda: settings_mod._render_personal_memory('English'))

    assert 'be terse' in pm.read_memory(pm.SCOPE_USER)['body']


def test_owner_profile_card_reaches_the_prompt(mock_env):
    """What the editor writes is what the prompt card renders."""
    import ui.pages.settings as settings_mod
    from core import personal_memory as pm

    mock_env._text_returns['settings_memory_text'] = (
        '## Rules\n- always answer in Russian\n\n## Notes\n- works from macOS'
    )
    mock_env.click('settings_memory_save')
    _invoke(lambda: settings_mod._render_personal_memory('English'))

    card = pm.render_owner_card()
    assert 'always answer in Russian' in card
    assert 'works from macOS' in card
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
