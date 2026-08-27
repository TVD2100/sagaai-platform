# -*- coding: utf-8 -*-
'''Scenario tests for the function-calling RAG assistant dialog.'''
import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

YANDEX_SERVICE = {
    'name': 'YandexAI',
    'config_key': 'yandex_iam_token',
    'config_key2': 'yandex_cloud_id',
    'auth_type': 'yandex_iam',
    'base_url': 'https://ai.api.cloud.yandex.net/v1',
}


def _function_call(name, arguments, call_id='call_1'):
    return {
        'type': 'function_call',
        'name': name,
        'call_id': call_id,
        'arguments': json.dumps(arguments, ensure_ascii=False),
    }


def _final_message(text):
    return {
        'type': 'message',
        'role': 'assistant',
        'content': [{'type': 'output_text', 'text': text}],
    }


def _mock_responses(*bodies):
    '''Serve several Responses API bodies via requests.post.'''
    seq = list(bodies)

    def _side_effect(*args, **kwargs):
        data = seq.pop(0)
        resp = MagicMock()
        resp.status_code = 200
        body = {
            'output': list(data),
            'usage': {'input_tokens': 10, 'output_tokens': 20},
        }
        resp.json.return_value = body
        return resp

    post = MagicMock()
    post.side_effect = _side_effect
    return post


@pytest.fixture()
def isolated_data(isolated_app_modules, monkeypatch, tmp_path):
    '''Fresh DATA_DIR for the scenario.'''
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('SAGAAI_DATA_DIR', str(data_dir))
    yield data_dir


def _load_default_assistant():
    '''Create a scoped replacement for the removed 'AI Studio Docs' preset:
    the same tool set (web_search + native rag_search) and the same bindings
    (rag base yaagentai_2020, web-search overrides).'''
    from core.assistants import create_assistant
    from core.assistant_folders import (
        set_assistant_rag_bases,
        set_assistant_web_search_settings,
    )
    from core.tools_utils import build_rag_search_tool

    pid = create_assistant(
        name='ScenarioRagBot', service='YandexAI', model='m',
        temperature=0.3, text='You search official docs.',
        tools=['web_search', build_rag_search_tool(['yaagentai_2020'])],
    )

    from core.assistants import get_assistant_by_id

    assistant = get_assistant_by_id(pid)
    assert assistant is not None, 'assistant was not created'
    assert set_assistant_rag_bases(assistant['slug'], ['yaagentai_2020'])
    assert set_assistant_web_search_settings(
        assistant['slug'], context_size='medium',
        allowed_domains=['yandex.cloud', 'aistudio.yandex.ru'],
    )
    return assistant


def _send_request(assistant, user_message, mock_post):
    '''Run one chat turn through the public send_request.'''
    from core.api_layer import send_request

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch('core.api_layer.get_services',
                  return_value={'YandexAI': YANDEX_SERVICE})
        )
        stack.enter_context(
            patch('core.api_layer.load_config',
                  return_value={'yandex_iam_token': 'iam-token',
                                'yandex_cloud_id': 'folder-id'})
        )
        stack.enter_context(
            patch('core.api_layer.load_skill_files_context', return_value='')
        )
        stack.enter_context(patch('core.api_layer.requests.post', mock_post))
        return send_request(user_message, assistant)


def test_rag_assistant_dialog_happy_path(isolated_data):
    '''Given the default assistant, when the user asks about docs, then the rag_search call is executed locally and the final answer is returned.'''
    assistant = _load_default_assistant()

    tools = assistant.get('tools') or []
    native = [t for t in tools
              if isinstance(t, dict) and t.get('type') == 'function']
    assert any(t.get('name') == 'rag_search' for t in native), tools
    from core.assistant_folders import load_assistant_bundle
    bundle = load_assistant_bundle(assistant['slug']) or {}
    assert bundle.get('rag_bases') == ['yaagentai_2020'], bundle

    first = [_function_call('rag_search',
                            {'slug': 'yaagentai_2020', 'query': 'лимиты токенов'})]
    final = [_final_message('Лимиты описаны в документации.')]
    mock_post = _mock_responses(first, final)

    fake_hits = [
        {'source': 'docs/limits.md', 'chunk_index': 0, 'score': 0.9,
         'text': 'Максимальный лимит токенов составляет N.'},
    ]
    with patch('core.assistant_tools.search_base',
               return_value=fake_hits) as search_mock:
        answer = _send_request(assistant, 'Какие лимиты токенов?', mock_post)

    assert answer == 'Лимиты описаны в документации.'
    first_payload = mock_post.call_args_list[0][1]['json']
    tool_types = [t.get('type') for t in first_payload['tools']]
    assert 'function' in tool_types
    assert search_mock.call_count == 1
    slug, query = search_mock.call_args[0][0], search_mock.call_args[0][1]
    assert slug == 'yaagentai_2020'
    assert 'лимиты' in query.lower()

    second_payload = mock_post.call_args_list[1][1]['json']
    input_types = [i.get('type') for i in second_payload['input']
                   if isinstance(i, dict)]
    assert 'function_call' in input_types
    assert 'function_call_output' in input_types
    outputs = [i for i in second_payload['input']
               if isinstance(i, dict) and i.get('type') == 'function_call_output']
    assert outputs and 'лимит' in str(outputs[0].get('output', '')).lower()


def test_rag_assistant_without_bases_has_no_function_tool(isolated_data):
    '''Given an assistant with no RAG bases bound, when the assistant is
    created without a native function tool, then the payload carries no
    function tools and the legacy web_search path is used.'''
    from core.assistants import create_assistant, get_assistant_by_id

    pid = create_assistant(
        name='NoRagBot', service='YandexAI', model='m', temperature=0.3,
        text='sys', tools=['web_search'],
    )
    assistant = get_assistant_by_id(pid)
    assert assistant is not None
    assert not any(
        isinstance(t, dict) and t.get('type') == 'function'
        for t in (assistant.get('tools') or [])
    )

    mock_post = _mock_responses([_final_message('OK')])
    answer = _send_request(assistant, 'Hello', mock_post)
    assert answer == 'OK'
    payload = mock_post.call_args_list[0][1]['json']
    tool_types = [t.get('type') for t in payload.get('tools', [])]
    assert 'function' not in tool_types
    assert payload.get('tool_choice') == {'type': 'web_search'}


def test_rag_assistant_manifest_web_search_overrides_in_payload(isolated_data):
    '''Given an assistant with per-assistant web-search settings, when a
    RAG dialog runs, then every payload carries the manifest overrides.'''
    from core.assistants import create_assistant, get_assistant_by_id
    from core.assistant_folders import (
        set_assistant_rag_bases,
        set_assistant_web_search_settings,
    )
    from core.tools_utils import build_rag_search_tool

    pid = create_assistant(
        name='OverrideBot', service='YandexAI', model='m', temperature=0.3,
        text='sys', tools=['web_search', build_rag_search_tool(['yaagentai_2020'])],
    )
    assistant = get_assistant_by_id(pid)
    assert assistant is not None
    set_assistant_rag_bases(assistant['slug'], ['yaagentai_2020'])
    set_assistant_web_search_settings(
        assistant['slug'], context_size='high',
        allowed_domains=['docs.example.org'],
    )

    first = [_function_call('rag_search',
                            {'slug': 'yaagentai_2020', 'query': 'лимиты'})]
    final = [_final_message('Найден ответ.')]
    mock_post = _mock_responses(first, final)
    fake_hits = [
        {'source': 'docs/limits.md', 'chunk_index': 0, 'score': 0.9,
         'text': 'Лимиты описаны.'},
    ]

    with patch('core.assistant_tools.search_base', return_value=fake_hits):
        answer = _send_request(assistant, 'Какие лимиты?', mock_post)

    assert answer == 'Найден ответ.'
    assert len(mock_post.call_args_list) == 2
    for _, kwargs in mock_post.call_args_list:
        payload = kwargs['json']
        web_tool = next(
            (t for t in payload.get('tools', []) if t.get('type') == 'web_search'),
            None,
        )
        assert web_tool is not None
        assert web_tool.get('search_context_size') == 'high'
        assert web_tool.get('filters', {}).get('allowed_domains') == [
            'docs.example.org'
        ]
        assert any(
            t.get('type') == 'function' and t.get('name') == 'rag_search'
            for t in payload.get('tools', [])
        )


def test_rag_assistant_rejects_unassigned_base(isolated_data):
    '''Given the assistant bound only to yaagentai_2020, when the model calls rag_search with another slug, then the platform answers access-denied.'''
    assistant = _load_default_assistant()

    first = [_function_call('rag_search',
                            {'slug': 'someone_elses_base', 'query': 'test'})]
    final = [_final_message('Не удалось найти.')]
    mock_post = _mock_responses(first, final)

    with patch('core.assistant_tools.search_base') as search_mock:
        answer = _send_request(assistant, 'Test', mock_post)

    assert answer == 'Не удалось найти.'
    search_mock.assert_not_called()
    second_payload = mock_post.call_args_list[1][1]['json']
    outputs = [i for i in second_payload['input']
               if isinstance(i, dict) and i.get('type') == 'function_call_output']
    assert outputs
    assert 'access denied' in str(outputs[0].get('output', '')).lower()
