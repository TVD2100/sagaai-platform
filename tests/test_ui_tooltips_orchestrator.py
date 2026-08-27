# -*- coding: utf-8 -*-
"""tests/test_ui_tooltips_orchestrator.py - orchestrator-settings tooltips.

Renders ui.pages.orchestrator_settings under the Streamlit mock and verifies
that the models, prompt, economy, function and instruction forms all carry
real non-empty tooltips. Shares the mock helpers with
tests/test_ui_tooltips.py.

Note: the orchestator prompt keeps its help as a visible caption
(orch_prompt_label_help), not as a widget tooltip - the label is hidden
(label_visibility=collapsed), so a visible caption is the better UX for
newcomers. The test covers both supports.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_ui_tooltips import (
    SAMPLE_SERVICE,
    _assert_real_help,
    _help_by_key,
    _invoke,
    mock_env,  # noqa: F401 - imported pytest fixture
)

ORCH = {
    'name': 'Test Orchestrator',
    'description': 'test orchestrator',
    'prompt_text': 'system prompt',
    'config': {
        'strong_service': 'TestSvc',
        'strong_model': 'm1',
        'strong_temperature': 0.4,
        'weak_service': 'TestSvc',
        'weak_model': 'm2',
        'weak_temperature': 0.4,
        'search_service': 'TestSvc',
        'search_model': 'm1',
        'search_temperature': 0.2,
        'web_search_prompt': '',
    },
}


def test_orchestrator_settings_widgets_have_tooltips(mock_env):
    'Models/prompt/economy/functions/instructions forms carry real tooltips.'
    import ui.pages.orchestrator as orch_mod
    import ui.pages.orchestrator_settings as settings_mod
    from core.i18n import t

    slug = 'test'
    mock_env.session_state.update({
        'orch_' + slug + '_show_func_form': True,
        'orch_' + slug + '_show_oinstr_form': True,
    })
    with patch.object(orch_mod, 'get_orchestrator', return_value=dict(ORCH)), \
         patch.object(settings_mod, 'get_orchestrator', return_value=dict(ORCH)), \
         patch.object(orch_mod, 'get_services', return_value={'TestSvc': SAMPLE_SERVICE}), \
         patch.object(orch_mod, 'get_economy_config',
                      return_value={'tail_messages': 8, 'cache_enabled': False,
                                    'cache_multiplier': 2}), \
         patch.object(orch_mod, 'get_economy_tail_messages', return_value=8), \
         patch.object(orch_mod, 'get_web_search_config', return_value={'prompt': ''}), \
         patch.object(orch_mod, 'orch_get_function', return_value=None), \
         patch.object(orch_mod, 'orch_list_functions', return_value=[]), \
         patch.object(orch_mod, 'orch_get_instruction', return_value=None), \
         patch.object(orch_mod, 'orch_list_instructions', return_value=[]), \
         patch.object(orch_mod, 'list_library_skills', return_value=[]), \
         patch.object(orch_mod, 'list_bases_with_activity', return_value=[]):
        _invoke(lambda: settings_mod.page_orchestrator_settings(slug))

    assert not mock_env.errors
    helps = _help_by_key(mock_env)
    _assert_real_help(helps, [
        'orch_set_strong_svc_' + slug,
        'orch_set_strong_mdl_' + slug,
        'orch_set_strong_temp_' + slug,
        'orch_set_strong_max_tokens_' + slug,
        'orch_set_weak_svc_' + slug,
        'orch_set_weak_mdl_' + slug,
        'orch_set_weak_temp_' + slug,
        'orch_set_weak_max_tokens_' + slug,
        'orch_set_search_svc_' + slug,
        'orch_set_search_mdl_' + slug,
        'orch_set_search_temp_' + slug,
        'orch_set_search_mtc_' + slug,
        'orch_set_search_prompt_' + slug,
        'orch_economy_tail_' + slug,
        'orch_economy_cache_' + slug,
        'orch_economy_multiplier_' + slug,
        'orch_func_name_' + slug,
        'orch_func_code_' + slug,
        'orch_oinstr_name_' + slug,
        'orch_oinstr_desc_' + slug,
        'orch_oinstr_prompt_' + slug,
    ], 'orchestrator settings')

    captions = [args[0] for name, args, _kwargs in mock_env.calls if name == 'caption']
    assert t('orch_prompt_label_help', lang='English') in captions
