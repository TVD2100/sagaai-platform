# tests/test_rag_tools_robustness.py
# Regression tests for rag_search argument robustness: wrong argument names
# and missing required arguments must produce structured errors with a
# 'suggestion' containing the exact expected signature.

import sys
import types

import pytest

from dev_agent import config
from dev_agent.tool_executor import ToolExecutor


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    # Redirect DevAgent state into an isolated temp sandbox.
    root = tmp_path / 'proj'
    root.mkdir(parents=True)
    monkeypatch.setattr(config, 'PROJECT_ROOT', root)
    monkeypatch.setattr(config, 'BACKUPS_DIR', root / 'dev_agent' / 'backups')
    monkeypatch.setattr(config, 'WORKSPACE_DIR', root / 'dev_agent' / 'workspace')
    monkeypatch.setattr(config, 'CHANGELOG_FILE', root / 'CHANGELOG.md')
    monkeypatch.setattr(config, 'PROTECTED_FILES', ())
    config.ensure_runtime_dirs()
    return root


def test_rag_search_rejects_legacy_arg_names_with_suggestion(sandbox):
    # Wrong argument names (base_id) return a structured error with a
    # suggestion containing the exact rag_search signature.
    te = ToolExecutor()
    res = te.rag_search(base_id='yaagentai_2020', query='x')
    assert not res['ok']
    assert 'unexpected argument' in res['error']
    assert 'base_id' in res['error']
    assert 'suggestion' in res
    assert 'rag_search(slug=' in res['suggestion']


def test_rag_search_missing_slug_has_suggestion(sandbox):
    # Missing slug returns an error plus the exact expected signature.
    te = ToolExecutor()
    res = te.rag_search(query='x')
    assert not res['ok']
    assert "'slug'" in res['error']
    assert 'suggestion' in res
    assert 'rag_search(slug=' in res['suggestion']


def test_rag_search_missing_query_has_suggestion(sandbox):
    # Missing query returns an error plus the exact expected signature.
    te = ToolExecutor()
    res = te.rag_search(slug='yaagentai_2020')
    assert not res['ok']
    assert "'query'" in res['error']
    assert 'suggestion' in res
    assert 'rag_search(slug=' in res['suggestion']


def test_rag_search_valid_call_reaches_backend(sandbox, monkeypatch):
    # With correct slug + query arguments the call reaches the search
    # backend (mocked here) instead of an argument error.
    fake = types.ModuleType('core.rag_search')
    fake.RagSearchError = type('RagSearchError', (Exception,), {})
    fake.search_base = lambda *a, **k: []
    fake.build_search_context = lambda *a, **k: ''
    monkeypatch.setitem(sys.modules, 'core.rag_search', fake)
    te = ToolExecutor()
    res = te.rag_search(slug='yaagentai_2020', query='golosovoy agent')
    assert res['ok'], res
    assert res['count'] == 0


# ─── rag_get_chunks robustness ──────────────────────────────────────────────

def test_rag_get_chunks_rejects_unknown_arg_names(sandbox):
    # Wrong argument names are rejected with the exact expected signature.
    te = ToolExecutor()
    res = te.rag_get_chunks(slug='b', base_id='x')
    assert not res['ok']
    assert 'unexpected argument' in res['error']
    assert 'base_id' in res['error']
    assert 'suggestion' in res
    assert 'rag_get_chunks(slug=' in res['suggestion']


def test_rag_get_chunks_missing_slug_has_suggestion(sandbox):
    te = ToolExecutor()
    res = te.rag_get_chunks(chunk_ids=[1])
    assert not res['ok']
    assert "'slug'" in res['error']
    assert 'rag_get_chunks(slug=' in res['suggestion']


def test_rag_get_chunks_requires_addressing_mode(sandbox):
    # Neither chunk_ids nor source+chunk_indices -> structured error.
    te = ToolExecutor()
    res = te.rag_get_chunks(slug='b')
    assert not res['ok']
    assert 'chunk_ids' in res['error']
    assert 'chunk_indices' in res['error']


def test_rag_get_chunks_valid_call_reaches_backend(sandbox, monkeypatch):
    fake = types.ModuleType('core.rag_search')
    fake.RagSearchError = type('RagSearchError', (Exception,), {})
    captured = {}

    def _get_chunks(slug, chunk_ids=None, source='', chunk_indices=None):
        captured.update(slug=slug, chunk_ids=chunk_ids, source=source,
                        chunk_indices=chunk_indices)
        return {'chunks': [{'chunk_id': 5, 'source': 'doc.md',
                            'chunk_index': 4, 'text': 'part five'}],
                'missing': []}

    fake.get_chunks = _get_chunks
    fake.build_search_context = lambda *a, **k: 'CTX'
    monkeypatch.setitem(sys.modules, 'core.rag_search', fake)
    te = ToolExecutor()
    res = te.rag_get_chunks(slug='b', chunk_ids=[5])
    assert res['ok'], res
    assert res['count'] == 1
    assert res['chunks'][0]['chunk_id'] == 5
    assert captured['slug'] == 'b'
    assert captured['chunk_ids'] == [5]
    assert 'CTX' in res['text']


def test_rag_get_chunks_backend_error_is_wrapped(sandbox, monkeypatch):
    fake = types.ModuleType('core.rag_search')
    fake.RagSearchError = type('RagSearchError', (Exception,), {})

    def _boom(*a, **k):
        raise fake.RagSearchError('base is not indexed yet')

    fake.get_chunks = _boom
    fake.build_search_context = lambda *a, **k: ''
    monkeypatch.setitem(sys.modules, 'core.rag_search', fake)
    te = ToolExecutor()
    res = te.rag_get_chunks(slug='b', chunk_ids=[1])
    assert not res['ok']
    assert 'not indexed' in res['error']


def test_rag_search_hits_carry_chunk_id(sandbox, monkeypatch):
    fake = types.ModuleType('core.rag_search')
    fake.RagSearchError = type('RagSearchError', (Exception,), {})
    fake.search_base = lambda *a, **k: [
        {'chunk_id': 7, 'source': 'a.md', 'chunk_index': 3,
         'score': 0.5, 'text': 'x'}]
    fake.build_search_context = lambda *a, **k: 'CTX'
    monkeypatch.setitem(sys.modules, 'core.rag_search', fake)
    te = ToolExecutor()
    res = te.rag_search(slug='b', query='q')
    assert res['ok'], res
    assert res['hits'][0]['chunk_id'] == 7
