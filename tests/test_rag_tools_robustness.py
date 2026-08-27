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
