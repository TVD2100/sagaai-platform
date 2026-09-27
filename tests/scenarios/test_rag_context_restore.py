# -*- coding: utf-8 -*-
"""tests/scenarios/test_rag_context_restore.py - scenarios for the RAG context-restore tool.

``rag_search`` results now carry chunk ids, and the new ``rag_get_chunks``
function tool pulls specific chunks of a base - by id, or by source file +
0-based positions - so both assistants and DevAgent can restore the context
around a hit instead of re-querying.

Scenarios (given -> when -> then):

  1. "assistant dialog" - an assistant bound to a real base answers a
     question: rag_search runs first (hit mocked), the model then calls
     rag_get_chunks over the real index and the restored chunk texts reach
     the follow-up payload before the final answer.
  2. "DevAgent tools" - the same chain through the DevAgent ToolExecutor:
     rag_search hits carry chunk ids and rag_get_chunks returns the stored
     texts in both addressing modes.
  3. "error states" - a missing base, a call without an addressing mode and
     an unbound assistant all return structured errors.
"""
from __future__ import annotations

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


@pytest.fixture()
def scenario_data(isolated_app_modules, monkeypatch, tmp_path):
    """Fresh DATA_DIR so every core module sees an empty runtime sandbox."""
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('SAGAAI_DATA_DIR', str(data_dir))
    yield data_dir


def _make_ready_base(chunks):
    """Create a ready RAG base with the given (source, chunk_index, text) rows."""
    from core import rag
    from core.rag_index import add_chunk

    base = rag.create_base(
        name='Context Restore KB', provider='YandexAI',
        embedding_model='text-search-doc', chunk_size=200, chunk_overlap=10,
    )
    slug = base['slug']
    rag.set_status(slug, 'ready')
    db = rag.index_db_path(slug)
    ids = [
        add_chunk(db, text, source=source, chunk_index=index)
        for source, index, text in chunks
    ]
    return slug, ids


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
    """Serve several Responses API bodies via requests.post."""
    seq = list(bodies)

    def _side_effect(*args, **kwargs):
        data = seq.pop(0)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            'output': list(data),
            'usage': {'input_tokens': 10, 'output_tokens': 20},
        }
        return resp

    post = MagicMock()
    post.side_effect = _side_effect
    return post


def _send_request(assistant, user_message, mock_post):
    """Run one chat turn through the public send_request."""
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


def test_assistant_restores_context_around_a_hit(scenario_data):
    """S1: the assistant walks search -> chunk fetch -> answer.

    given: an assistant bound to a real base with three chunks of one file
    when: the model first calls rag_search (hit mocked), then rag_get_chunks
          addressing the same file by source + positions, and finally
          answers
    then: both calls execute locally, the follow-up payload carries the
          restored chunk texts from the real index and the answer returns
    """
    slug, ids = _make_ready_base([
        ('doc.md', 0, 'part one: general overview'),
        ('doc.md', 1, 'part two: token limits and quotas'),
        ('doc.md', 2, 'part three: rate limits'),
    ])

    from core.assistants import create_assistant, get_assistant_by_id
    from core.assistant_folders import set_assistant_rag_bases
    from core.tools_utils import build_rag_chunks_tool, build_rag_search_tool

    pid = create_assistant(
        name='ContextBot', service='YandexAI', model='m',
        temperature=0.3, text='You restore context.',
        tools=['web_search', build_rag_search_tool([slug]),
               build_rag_chunks_tool([slug])],
    )
    assistant = get_assistant_by_id(pid)
    assert assistant is not None
    assert set_assistant_rag_bases(assistant['slug'], [slug])

    first = [_function_call('rag_search',
                            {'slug': slug, 'query': 'token limits'})]
    second = [_function_call('rag_get_chunks',
                             {'slug': slug, 'source': 'doc.md',
                              'chunk_indices': [0, 1, 2]},
                             call_id='call_2')]
    final = [_final_message('Limits are described in the limits section.')]
    mock_post = _mock_responses(first, second, final)

    fake_hits = [
        {'chunk_id': ids[1], 'source': 'doc.md', 'chunk_index': 1,
         'score': 0.9, 'text': 'part two: token limits and quotas'},
    ]
    with patch('core.assistant_tools.search_base',
               return_value=fake_hits) as search_mock:
        answer = _send_request(assistant, 'What are the token limits?',
                               mock_post)

    assert answer == 'Limits are described in the limits section.'
    assert search_mock.call_count == 1
    assert len(mock_post.call_args_list) == 3

    # The third request carries the restored chunk texts from the real index.
    third_payload = mock_post.call_args_list[2][1]['json']
    outputs = [i for i in third_payload['input']
               if isinstance(i, dict)
               and i.get('type') == 'function_call_output']
    assert len(outputs) == 2, outputs
    chunk_output = outputs[-1]['output']
    assert 'part two: token limits and quotas' in chunk_output
    assert 'part three: rate limits' in chunk_output


def test_devagent_fetch_follows_search_ids(scenario_data):
    """S2: DevAgent search hits carry ids and fetch restores neighbours.

    given: a ready base and the DevAgent ToolExecutor
    when: rag_search runs over it (hits mocked) and the returned chunk id is
          passed to rag_get_chunks on the real index; a source window is
          fetched afterwards
    then: the fetched text matches the stored chunk and the window returns
          the requested positions in order
    """
    slug, ids = _make_ready_base([
        ('alpha.md', 0, 'alpha opening section'),
        ('alpha.md', 1, 'alpha middle section'),
        ('alpha.md', 2, 'alpha closing section'),
    ])

    from dev_agent.tool_executor import ToolExecutor

    fake_hits = [
        {'chunk_id': ids[1], 'source': 'alpha.md', 'chunk_index': 1,
         'score': 0.77, 'text': 'alpha middle section'},
    ]
    te = ToolExecutor()
    with patch('core.rag_search.search_base', return_value=fake_hits):
        search = te.rag_search(slug=slug, query='middle')
    assert search['ok'], search
    hit_id = search['hits'][0]['chunk_id']
    assert hit_id == ids[1]

    fetched = te.rag_get_chunks(slug=slug, chunk_ids=[hit_id])
    assert fetched['ok'], fetched
    assert fetched['count'] == 1
    assert fetched['chunks'][0]['chunk_id'] == ids[1]
    assert 'alpha middle section' in fetched['text']

    window = te.rag_get_chunks(slug=slug, source='alpha.md',
                               chunk_indices=[0, 2])
    assert window['ok'], window
    assert [c['chunk_index'] for c in window['chunks']] == [0, 2]
    assert 'alpha opening section' in window['text']
    assert 'alpha closing section' in window['text']


def test_error_states_stay_structured(scenario_data):
    """S3: missing base, missing addressing mode and access denial.

    given: a ready base, the DevAgent executor and an assistant without
           bound bases
    when: rag_get_chunks is called with an unknown slug, without an
          addressing mode, and by the unbound assistant
    then: each path returns a structured ok=false payload with a clear
          error instead of raising
    """
    slug, _ids = _make_ready_base([('solo.md', 0, 'solo chunk')])

    from dev_agent.tool_executor import ToolExecutor

    te = ToolExecutor()
    missing = te.rag_get_chunks(slug='no_such_base_xyz', chunk_ids=[1])
    assert not missing['ok']
    assert 'does not exist' in missing['error'].lower()

    no_mode = te.rag_get_chunks(slug=slug)
    assert not no_mode['ok']
    assert 'chunk_ids' in no_mode['error']

    from core.assistants import create_assistant, get_assistant_by_id

    pid = create_assistant(
        name='UnboundBot', service='YandexAI', model='m',
        temperature=0.3, text='sys', tools=['web_search'],
    )
    assistant = get_assistant_by_id(pid)
    assert assistant is not None

    from core.assistant_tools import execute_assistant_rag_chunks

    denied = json.loads(execute_assistant_rag_chunks(
        {'slug': slug, 'chunk_ids': [1]}, assistant=assistant))
    assert denied['ok'] is False
    assert 'access denied' in denied['error'].lower()
