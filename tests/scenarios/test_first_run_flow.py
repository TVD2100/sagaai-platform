# -*- coding: utf-8 -*-
"""tests/scenarios/test_first_run_flow.py - first-run scenario tests.

Simulates a newcomer's first journey through SagaAI under the Streamlit
mock. Each scenario is written in given -> when -> then form and walks the
UI through public entry points (ui.app.main and the real page render
functions), so prompts/injections never bypass the application flow.

  Scenario 1 - welcome walkthrough: the welcome page renders the four
               onboarding cards, and clicking the providers card navigates
               to the settings page.
  Scenario 2 - guided provider setup: the settings page explains every
               API-key field with a provider-specific tooltip resolved from
               key_help/key2_help in the real service JSONs, and a pasted
               key is persisted when the newcomer clicks Save.
  Scenario 3 - first assistant: the assistant create form explains every
               field, and saving a filled form persists the assistant
               (name, prompt, model) in the real database.
  Scenario 4 - skills library: the install tabs explain every field, the
               GitHub URL placeholder is vendor-neutral (no
               github.com/anthropics/skills example link anywhere), and
               the page renders without errors.

These scenarios back tier 3 (scenario testing) of the tooltip-audit task
and double as regression suite members.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402

BANNED_LINK = 'github.com/anthropics/skills'

# Minimal service definition for UI forms. Mirrors SAMPLE_SERVICE from
# tests/test_ui_tooltips.py (kept local so the scenario file stays
# self-contained).
SAMPLE_SERVICE = {
    'auth_type': 'bearer',
    'base_url': 'https://mock',
    'config_key': 'test_key',
    'key_label': 'API Key',
    'models': [
        {'id': 'm1', 'max_tokens': 32000, 'context_window': 128000},
        {'id': 'm2', 'max_tokens': 32000, 'context_window': 128000},
    ],
    'temp_min': 0.0,
    'temp_max': 1.0,
    'temp_step': 0.05,
    'temp_default': 0.7,
    'max_tokens_default': 32000,
    'tools_options': [{'key': 'web_search'}],
}


@pytest.fixture()
def isolated_data(isolated_app_modules, monkeypatch, tmp_path):
    """Fresh DATA_DIR + fresh app modules, matching the smoke-test pattern."""
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('SAGAAI_DATA_DIR', str(data_dir))
    yield data_dir


def _render(fn):
    """Run a render function, swallowing the expected StopRerun."""
    try:
        fn()
    except StopRerun:
        pass


def _help_by_key(st):
    """Map widget key -> help text across all recorded calls."""
    return {
        kwargs.get('key'): kwargs.get('help')
        for _name, _args, kwargs in st.calls
        if kwargs.get('key')
    }


def _all_strings(st):
    """Return every plain-string argument recorded by the mock."""
    strings = []
    for _name, args, kwargs in st.calls:
        for value in args:
            if isinstance(value, str):
                strings.append(value)
        for value in kwargs.values():
            if isinstance(value, str):
                strings.append(value)
    return strings


# ─── Scenario 1: welcome walkthrough ----------------------------------------

def test_first_run_welcome_walkthrough(isolated_data):
    """
    Given a fresh install opened on the welcome page,
    when the newcomer reads the onboarding cards and clicks the
    providers card,
    then the app navigates to the settings page without errors and all
    four step cards are visible.
    """
    with install_streamlit_mock() as st:
        app_mod = importlib.import_module('ui.app')
        st.session_state.update({
            '_defaults_seeded': True,
            'ui_lang': 'English',
            'current_page': 'welcome',
        })
        _render(app_mod.main)
        assert st.errors == [], 'welcome render emitted errors: %r' % st.errors

        rendered_keys = {kwargs.get('key') for _n, _a, kwargs in st.calls}
        for step_key in (
            'welcome_step_settings',
            'welcome_step_orchestrator_settings:dev_agent',
            'welcome_step_skills',
            'welcome_step_orchestrators',
        ):
            assert step_key in rendered_keys, 'missing onboarding button ' + step_key

        st.reset_clicks()
        st.click('welcome_step_settings')
        _render(app_mod.main)
        assert st.session_state['current_page'] == 'settings'
        assert st.errors == []


# ─── Scenario 2: guided provider setup --------------------------------------

def test_settings_explain_keys_and_save_them(isolated_data):
    """
    Given real service definitions with localized key_help/key2_help and a
    newcomer who pastes an API key into the first provider form,
    when they click Save,
    then every key field carries the provider-specific tooltip from the
    service JSONs and the pasted key reaches the saved config.
    """
    with install_streamlit_mock() as st:
        settings_mod = importlib.import_module('ui.pages.settings')
        from core import services as core_services

        services = core_services.discover_services()
        expected = {}
        for svc_name, svc in services.items():
            key1 = svc.get('config_key')
            if key1 and svc.get('key_help'):
                expected['cfg_' + key1] = svc['key_help']['en']
            key2 = svc.get('config_key2')
            if key2 and svc.get('key2_label') and svc.get('key2_help'):
                expected['cfg_' + key2] = svc['key2_help']['en']
        assert len(expected) >= 4, 'expected >=4 service key widgets, got %r' % expected

        # First service with a key field is the one the newcomer fills in.
        target_svc = next(
            name for name, svc in services.items() if svc.get('key_help')
        )
        target_cfg_key = services[target_svc]['config_key']
        st._text_returns['cfg_' + target_cfg_key] = 'sk-scenario-key'

        saved_configs = []

        def _fake_save(cfg):
            saved_configs.append(dict(cfg))
            return True

        with patch.object(settings_mod, 'load_config', return_value={}), \
             patch.object(settings_mod, 'has_key', return_value=False), \
             patch.object(settings_mod, 'is_env_key_set_for_service',
                          return_value=False), \
             patch.object(settings_mod, 'list_env_keys', return_value={}), \
             patch.object(settings_mod, 'save_config', side_effect=_fake_save), \
             patch.object(settings_mod, 'test_connection',
                          return_value=(True, 'ok')):
            _render(settings_mod.page_settings)
            st.reset_clicks()
            st.click('settings_save_' + target_svc)
            _render(settings_mod.page_settings)

    assert st.errors == []
    helps = _help_by_key(st)
    for key, expected_text in expected.items():
        assert key in helps, 'service key widget %r was not rendered' % key
        assert helps[key] == expected_text, (
            'tooltip for %r mismatch: %r != %r' % (key, helps[key], expected_text)
        )

    assert saved_configs, 'save_config was never called'
    assert saved_configs[-1].get(target_cfg_key) == 'sk-scenario-key'


# ─── Scenario 3: first assistant --------------------------------------------

def test_create_first_assistant_with_real_storage(isolated_data):
    """
    Given an empty install and the assistant create form opened,
    when the newcomer fills in a name and a system prompt and clicks Save,
    then every form field carries a non-empty tooltip and the assistant is
    persisted into the real database with its prompt and model intact.
    """
    prompt_text = 'You are a helpful writing assistant.'

    with install_streamlit_mock() as st:
        assistants_mod = importlib.import_module('ui.pages.assistants')
        st.session_state.update({
            'ui_lang': 'English',
            'show_assistant_form': True,
            'show_skill_form': True,
            'edit_assistant_id': None,
            'edit_skill_id': None,
            'assistant_prompt_revision': 0,
            'assistant_prompt_revision_for': None,
        })
        st._text_returns['assistant_name_input'] = 'First Assistant'
        st._text_returns['assistant_prompt_text_0'] = prompt_text

        with patch.object(assistants_mod, 'get_services',
                          return_value={'TestSvc': SAMPLE_SERVICE}), \
             patch.object(assistants_mod, 'list_tool_definitions',
                          return_value=[]), \
             patch.object(assistants_mod, 'service_supported_tools',
                          return_value=['web_search']), \
             patch.object(assistants_mod, 'service_supports_reasoning_effort',
                          return_value=False), \
             patch.object(assistants_mod, 'list_rag_bases', return_value=[]):
            _render(assistants_mod.page_assistants)
            assert st.errors == []

            helps = _help_by_key(st)
            for key in (
                'assistant_name_input',
                'assistant_desc',
                'assistant_service',
                'assistant_model',
                'assistant_temp',
                'assistant_max_calls',
                'assistant_max_tokens',
            ):
                value = helps.get(key)
                assert isinstance(value, str) and value.strip(), \
                    'missing tooltip for %r' % key
                assert value != key, 'raw i18n key leaked for %r' % key

            st.reset_clicks()
            st.click('assistant_save_btn')
            _render(assistants_mod.page_assistants)

    assert st.errors == []

    import core.assistants as core_assistants
    rows = core_assistants.load_assistants_index()
    created = [row for row in rows if row['name'] == 'First Assistant']
    assert created, 'assistant was not persisted: %r' % rows
    full = core_assistants.get_assistant_by_id(created[0]['id'])
    assert full is not None
    assert full.get('text') == prompt_text
    assert full.get('model') == 'm1'
    assert full.get('service') == 'TestSvc'


# ─── Scenario 4: skills library stays vendor-neutral ------------------------

def test_skills_library_install_forms_and_neutral_placeholder(isolated_data):
    """
    Given an empty skills library opened by a newcomer,
    when the install tabs (ZIP / GitHub / folder) are rendered,
    then every install field carries a tooltip, the GitHub URL placeholder
    is the neutral example, and no string on the page advertises the
    anthropics/skills repository.
    """
    with install_streamlit_mock() as st:
        slib_mod = importlib.import_module('ui.pages.skills_library')
        st.session_state.update({'ui_lang': 'English'})
        with patch.object(slib_mod, 'list_skills', return_value=[]), \
             patch.object(slib_mod, 'get_skills_root', return_value='/tmp/skills'):
            _render(slib_mod.page_skills_library)

    assert st.errors == []
    helps = _help_by_key(st)
    for key in ('slib_zip_upload', 'slib_gh_url', 'slib_gh_name',
                'slib_gh_desc', 'slib_folder_path'):
        value = helps.get(key)
        assert isinstance(value, str) and value.strip(), \
            'missing tooltip for %r' % key
        assert value != key, 'raw i18n key leaked for %r' % key

    gh_inputs = [
        kwargs for name, _args, kwargs in st.calls
        if name == 'text_input' and kwargs.get('key') == 'slib_gh_url'
    ]
    assert gh_inputs, 'GitHub URL input was not rendered'
    assert gh_inputs[-1].get('placeholder') == 'https://github.com/owner/repository'

    rendered_strings = _all_strings(st)
    assert not any(BANNED_LINK in text for text in rendered_strings), (
        'banned anthropics example link reappeared in the skill library UI'
    )
