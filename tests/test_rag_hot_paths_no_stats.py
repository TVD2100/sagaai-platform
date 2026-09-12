# -*- coding: utf-8 -*-
"""tests/test_rag_hot_paths_no_stats.py - hot paths avoid per-base index I/O.

Step 3 of the large-RAG-base performance work. Every render/context path that
does NOT display index statistics must request ``with_stats=False`` so the
per-base SQLite index is never opened when that path runs. These tests
monkeypatch the ``core.rag`` API (and the module-level imports of the UI
pages) and assert each hot path passes ``with_stats=False``:

- core.orchestrators._extend_prompt_with_rag_bases (orchestrator prompts)
- ui.pages.orchestrator._render_orch_rag_bases  (orchestrator settings page)
- dev_agent.tool_executor.ToolExecutor.list_rag_bases  (RAG tool)
- ui.pages.assistants.page_assistants  (assistants page)
- core.api_layer._assistant_rag_context  (auto-RAG chat context)
- core.rag_search.search_base  (semantic search validation)
- core.rag_indexer.index_base  (indexing pipeline)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


def _invoke(fn):
    try:
        fn()
    except StopRerun:
        pass


def _st_ctx():
    """Fresh Streamlit mock with any cached ui.* modules removed."""
    for name in list(sys.modules):
        if name == 'ui' or name.startswith('ui.'):
            sys.modules.pop(name, None)
    return install_streamlit_mock()


def test_extend_prompt_with_rag_bases_uses_no_stats():
    """Orchestrator prompt block must not open the SQLite indexes."""
    from core import orchestrators

    with patch('core.rag.list_bases_with_activity',
               return_value=[]) as mock_lb:
        out = orchestrators._extend_prompt_with_rag_bases(
            'System prompt', orchestrator_slug='dev_agent')
    assert out == 'System prompt'
    mock_lb.assert_called_once_with(with_stats=False)


def test_orchestrator_page_rag_section_uses_no_stats():
    """The orchestrator-settings RAG section renders without index I/O."""
    with _st_ctx() as st:
        st.session_state.update(ui_lang='English')
        import ui.pages.orchestrator as orch_mod

        with patch.object(orch_mod, 'list_bases_with_activity',
                          return_value=[]) as mock_lb:
            _invoke(lambda: orch_mod._render_orch_rag_bases('dev_agent',
                                                            'English'))
    mock_lb.assert_called_once_with(with_stats=False)


def test_devagent_list_rag_bases_tool_uses_no_stats():
    """The DevAgent list_rag_bases tool returns manifest metadata only."""
    from dev_agent.tool_executor import ToolExecutor

    with patch('core.rag.list_bases_with_activity',
               return_value=[]) as mock_lb:
        result = ToolExecutor().list_rag_bases()
    assert result.get('ok') is True
    assert result.get('count') == 0
    mock_lb.assert_called_once_with(with_stats=False)


def test_assistants_page_uses_no_stats():
    """The assistants page RAG multiselect renders without index I/O."""
    from tests.test_ui_tooltips import SAMPLE_SERVICE

    with _st_ctx() as st:
        st.session_state.update({
            'ui_lang': 'English',
            'show_assistant_form': True,
            'show_skill_form': True,
            'edit_assistant_id': None,
            'edit_skill_id': None,
        })
        import ui.pages.assistants as assistants_mod

        with patch.object(assistants_mod, 'get_services',
                          return_value={'TestSvc': SAMPLE_SERVICE}), \
             patch.object(assistants_mod, 'list_tool_definitions',
                          return_value=[]), \
             patch.object(assistants_mod, 'service_supported_tools',
                          return_value=['web_search']), \
             patch.object(assistants_mod, 'list_rag_bases',
                          return_value=[]) as mock_lr:
            _invoke(assistants_mod.page_assistants)
    mock_lr.assert_called_with(with_stats=False)


def test_assistant_rag_context_uses_no_stats():
    """The auto-RAG chat context builder needs only rag_slots metadata."""
    import core.api_layer as api

    with patch('core.assistant_folders.load_assistant_bundle',
               return_value={'rag_bases': ['kb1']}), \
         patch('core.rag.get_base',
               return_value={'slug': 'kb1', 'status': 'ready',
                             'rag_slots': []}) as mock_gb, \
         patch('core.rag_search.chat_context', return_value=''):
        ctx = api._assistant_rag_context(
            {'id': 'a1', 'slug': 'assist1', 'name': 'Assistant'}, 'hello')
    assert ctx == ''
    mock_gb.assert_called_once_with('kb1', with_stats=False)


def test_search_base_uses_no_stats():
    """Search validation reads the manifest without the index_stats block."""
    import core.rag_search as rs

    with patch('core.rag.get_base',
               return_value={'slug': 'kb1', 'status': 'ready',
                             'provider': 'yandex',
                             'embedding_model': 'text-search-doc'}) as mock_gb:
        hits = rs.search_base('kb1', '')
    assert hits == []
    mock_gb.assert_called_once_with('kb1', with_stats=False)


def test_index_base_uses_no_stats():
    """The indexing pipeline reads chunking parameters without index stats."""
    import core.rag_indexer as idx

    with patch('core.rag.get_base', return_value={}) as mock_gb:
        with pytest.raises(idx.IndexingError):
            idx.index_base('kb1')
    mock_gb.assert_called_once_with('kb1', with_stats=False)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
