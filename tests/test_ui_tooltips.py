# -*- coding: utf-8 -*-
"""tests/test_ui_tooltips.py - targeted tooltip regression tests.

Verifies the tooltip-audit work end-to-end at the UI layer.

Level 1 - i18n checks: every i18n key referenced as a widget tooltip
(help=t(...)) in the touched UI pages exists, is non-empty and does not
fall back to the raw key in every bundled language file (defaults/langs/ +
legacy langs/ copies); the removed anthropics skills example link is gone;
the DevAgent search-model help does not advertise a concrete provider.

Level 2 - widget checks: rendering each touched page under the Streamlit
mock produces real non-empty tooltips for the audited widgets: settings
API-key inputs, assistant form, skills library forms, RAG storage widgets
and orchestrator settings.

The Streamlit mock technique follows tests/test_ui_pages.py, but core.i18n.t
is intentionally NOT mocked so the real translations are exercised.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BANNED_LINK = 'github.com/anthropics/skills'

PAGES = [
    'ui/pages/settings.py',
    'ui/pages/assistants.py',
    'ui/pages/skills_library.py',
    'ui/pages/storage.py',
    'ui/pages/orchestrator.py',
    'ui/pages/orchestrator_settings.py',
]


# ---- i18n / service-file level checks ------------------------------------

def _lang_files():
    'Return every bundled language JSON file (defaults/langs + legacy langs).'
    files = []
    for directory in ('defaults/langs', 'langs'):
        path = ROOT / directory
        if path.is_dir():
            files.extend(sorted(path.glob('*.json')))
    return files


def _service_files():
    'Return every service definition JSON file (defaults/services + services).'
    files = []
    for directory in ('defaults/services', 'services'):
        path = ROOT / directory
        if path.is_dir():
            files.extend(sorted(path.glob('*.json')))
    return files


def _ui_help_keys():
    'Collect every i18n key passed as help=t(...) in the touched UI pages.'
    keys = set()
    for rel in PAGES:
        tree = ast.parse((ROOT / rel).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != 'help' or not isinstance(kw.value, ast.Call):
                    continue
                inner = kw.value
                if not isinstance(inner.func, ast.Name) or inner.func.id != 't':
                    continue
                if inner.args and isinstance(inner.args[0], ast.Constant):
                    value = inner.args[0].value
                    if isinstance(value, str) and value:
                        keys.add(value)
    assert keys, 'expected to find help=t(...) references in the UI pages'
    return keys


def test_ui_tooltip_keys_exist_and_are_non_empty_in_all_langs():
    'Every UI tooltip key must exist, be non-empty and not be the raw key.'
    files = _lang_files()
    assert len(files) >= 6, 'expected 6 language files (3 langs x 2 copies)'
    ui_keys = _ui_help_keys()
    for fpath in files:
        data = json.loads(fpath.read_text(encoding='utf-8'))
        problems = []
        for key in sorted(ui_keys):
            value = str(data.get(key) or '').strip()
            if not value:
                problems.append(key + ': missing/empty')
            elif value == key:
                problems.append(key + ': raw key returned')
        assert not problems, fpath.name + ': ' + str(problems)


def test_no_anthropics_skills_example_link():
    'The anthropics skills example link must be gone from every lang/service file.'
    for fpath in _lang_files() + _service_files():
        text = fpath.read_text(encoding='utf-8')
        assert BANNED_LINK not in text, str(fpath) + ': still contains ' + BANNED_LINK


def test_devagent_search_model_help_does_not_advertise_provider():
    'The DevAgent search-model help must not promote a concrete provider.'
    data = json.loads((ROOT / 'defaults' / 'langs' / 'en.json').read_text(encoding='utf-8'))
    help_text = str(data.get('devagent_search_model_help') or '')
    assert help_text.strip()
    lowered = help_text.lower()
    for vendor in ('yandex', 'deepseek', 'gigachat', 'openai', 'anthropic'):
        assert vendor not in lowered, 'advertises ' + vendor + ': ' + repr(help_text)


# ---- shared mock helpers ---------------------------------------------------

@pytest.fixture
def mock_env():
    'Fresh Streamlit mock with real core.i18n; ui.* is re-imported per test.'
    for name in list(sys.modules):
        if name == 'ui' or name.startswith('ui.'):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as st:
        st.session_state.update(ui_lang='English')
        yield st


def _invoke(fn):
    try:
        fn()
    except StopRerun:
        pass


def _help_by_key(st):
    return {
        kwargs.get('key'): kwargs.get('help')
        for _name, _args, kwargs in st.calls
        if kwargs.get('key')
    }


def _assert_real_help(helps, keys, source):
    'Every listed widget must carry a real non-empty, non-raw tooltip.'
    for key in keys:
        assert key in helps, source + ': widget ' + repr(key) + ' was not rendered'
        value = helps[key]
        assert isinstance(value, str) and value.strip(), source + ': empty help for ' + repr(key)
        assert value != key, source + ': raw i18n key leaked for ' + repr(key)
        assert not value.startswith('{'), source + ': unformatted placeholder for ' + repr(key)


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
    'extra_fields': [
        {
            'key': 'reasoning_effort',
            'type': 'select',
            'options': ['none', 'low', 'medium', 'high', 'max'],
            'default': 'max',
            'tooltip': {'en': 'Reasoning depth for this provider.'},
        },
    ],
}


# ---- settings page: API-key tooltips from service definitions -------------

def test_settings_api_key_widgets_use_service_key_help(mock_env):
    'The settings page resolves key_help/key2_help from the real service JSONs.'
    from core.services import discover_services
    import ui.pages.settings as settings_mod

    services = discover_services()
    expected = {}
    for svc_name, svc in services.items():
        key1 = svc.get('config_key')
        if key1 and svc.get('key_help'):
            expected['cfg_' + key1] = svc['key_help']['en']
        key2 = svc.get('config_key2')
        if key2 and svc.get('key2_label') and svc.get('key2_help'):
            expected['cfg_' + key2] = svc['key2_help']['en']
    assert len(expected) >= 4, 'expected at least 4 wired key widgets, got ' + str(expected)

    with patch.object(settings_mod, 'load_config', return_value={}), \
         patch.object(settings_mod, 'has_key', return_value=False), \
         patch.object(settings_mod, 'is_env_key_set_for_service', return_value=False), \
         patch.object(settings_mod, 'list_env_keys', return_value={}):
        _invoke(settings_mod.page_settings)

    assert not mock_env.errors
    helps = _help_by_key(mock_env)
    assert {key: helps[key] for key in expected} == expected


# ---- assistants page: create form tooltips ----------------------------------

def test_assistant_form_widgets_have_tooltips(mock_env):
    'Every field of the assistant create form renders a real tooltip.'
    import ui.pages.assistants as assistants_mod

    mock_env.session_state.update({
        'show_assistant_form': True,
        'show_skill_form': True,
        'edit_assistant_id': None,
        'edit_skill_id': None,
    })
    with patch.object(assistants_mod, 'get_services',
                      return_value={'TestSvc': SAMPLE_SERVICE}), \
         patch.object(assistants_mod, 'list_tool_definitions', return_value=[]), \
         patch.object(assistants_mod, 'service_supported_tools',
                      return_value=['web_search']), \
         patch.object(assistants_mod, 'list_rag_bases', return_value=[]):
        _invoke(assistants_mod.page_assistants)

    assert not mock_env.errors
    helps = _help_by_key(mock_env)
    _assert_real_help(helps, [
        'assistant_name_input',
        'assistant_desc',
        'assistant_service',
        'assistant_model',
        'assistant_temp',
        'assistant_reasoning_effort',
        'assistant_max_calls',
        'assistant_max_tokens',
    ], 'assistant form')

    prompt_calls = [
        kwargs
        for name, _args, kwargs in mock_env.calls
        if name == 'text_area'
        and str(kwargs.get('key', '')).startswith('assistant_prompt_text_')
    ]
    assert prompt_calls, 'assistant prompt text_area was not rendered'
    for kwargs in prompt_calls:
        help_text = kwargs.get('help')
        assert isinstance(help_text, str) and help_text.strip()
        assert help_text != 'prompt_text_help'

    tools_calls = [
        kwargs
        for name, _args, kwargs in mock_env.calls
        if name == 'multiselect' and kwargs.get('key') == 'assistant_tools_TestSvc'
    ]
    assert len(tools_calls) == 1, 'tools multiselect was not rendered'
    assert tools_calls[0].get('help') and tools_calls[0].get('help') != 'tools_help'


# ---- skills library: install form tooltips ----------------------------------

def test_skills_library_widgets_have_tooltips(mock_env):
    'ZIP / GitHub / folder install tabs carry tooltips; the placeholder is neutral.'
    import ui.pages.skills_library as slib_mod

    with patch.object(slib_mod, 'list_skills', return_value=[]), \
         patch.object(slib_mod, 'get_skills_root', return_value='/tmp/skills'):
        _invoke(slib_mod.page_skills_library)

    assert not mock_env.errors
    helps = _help_by_key(mock_env)
    for key in ('slib_zip_upload', 'slib_gh_url', 'slib_folder_path'):
        assert key in helps, 'skills library: ' + key + ' was not rendered'
        value = helps[key]
        assert isinstance(value, str) and value.strip() and value != key

    gh_placeholders = [
        kwargs.get('placeholder')
        for name, _args, kwargs in mock_env.calls
        if name == 'text_input' and kwargs.get('key') == 'slib_gh_url'
    ]
    assert gh_placeholders, 'GitHub URL input was not rendered'
    assert BANNED_LINK not in str(gh_placeholders[0])


# ---- storage page: search / chunk editor tooltips ---------------------------

def test_storage_widgets_have_tooltips(mock_env):
    'Test-search, chunk-search and chunk-text widgets carry tooltips.'
    import types
    import ui.pages.storage as storage_mod

    storage_mod.rag = types.SimpleNamespace(
        list_bases_with_activity=lambda: [],
        list_chunks=lambda *a, **k: {'total': 0, 'chunks': []},
    )

    _invoke(storage_mod.page_storage)
    _invoke(lambda: storage_mod._render_test_search('kb', 'English'))
    _invoke(lambda: storage_mod._render_chunks_section('kb', 'English'))
    _invoke(lambda: storage_mod._render_chunk_editor('kb', 1, {'text': 'x'}, 'English'))

    assert not mock_env.errors
    helps = _help_by_key(mock_env)
    _assert_real_help(helps, ['storage_tq_kb', 'storage_cq_kb', 'storage_ce_area_kb_1'],
                      'storage page')
