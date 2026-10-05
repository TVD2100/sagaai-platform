"""
Scenario tests for the context-overflow protection (M1/M4/M5, C1),
walking the real public paths end-to-end like a user would:
  - happy path: a 1.65M-character tool_result is spilled to disk before
    the first send and the model gets a bounded head+tail preview with a
    spill_path - the payload never reaches the model context;
  - error state: when the spill write fails, a payload beyond the hard
    cap yields an explicit ok=False size error;
  - edge case: a long conversation history is trimmed from the front
    BEFORE a provider round-trip;
  - error state: an impossible payload raises a clear
    ContextWindowError instead of an opaque HTTP 400.
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_ROOT = os.path.dirname(HERE)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

import core.api_layer as api_layer
from core.api_errors import ContextWindowError
from dev_agent import config
from dev_agent.agent_loop import _TOOL_RESULT_INLINE_LIMIT
from dev_agent.tool_executor import ToolExecutor


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect DevAgent state into a temp sandbox."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    monkeypatch.setattr(config, "PROJECT_ROOT", root)
    monkeypatch.setattr(config, "BACKUPS_DIR", root / "dev_agent" / "backups")
    monkeypatch.setattr(config, "WORKSPACE_DIR", root / "dev_agent" / "workspace")
    monkeypatch.setattr(config, "CHANGELOG_FILE", root / "CHANGELOG.md")
    monkeypatch.setattr(config, "PROTECTED_FILES", ())
    config.ensure_runtime_dirs()
    return root


def test_scenario_giant_tool_result_is_spilled_before_context(sandbox, monkeypatch):
    """Given a tool that returns a 1.65M-character payload, when the tool
    executor dispatches it, then the full payload is spilled to
    .dev_agent/tool_results/ before the first send and the model receives
    only a bounded head+tail preview with a recoverable spill_path."""
    big_payload = 'x' * 1_650_000
    executor = ToolExecutor()

    def _fake_list_files(subdir="", max_depth=1):
        return {
            'ok': True,
            'subdir': subdir or '.',
            'content': big_payload,
        }

    monkeypatch.setattr(executor, 'list_files', _fake_list_files)

    result = executor.dispatch('list_files', {'subdir': '.'})

    assert result['ok'] is True
    assert result['truncated'] is True
    assert result['bulk_sizes']['content'] > 1_000_000
    wire = json.dumps({'tool_result': result}, ensure_ascii=False)
    assert len(wire) <= _TOOL_RESULT_INLINE_LIMIT
    assert big_payload not in wire
    # The full payload is recoverable: the spill file holds it completely
    # and the public read_file tool can open the spill path.
    spill_text = (sandbox / result['spill_path']).read_text(encoding='utf-8')
    assert big_payload in spill_text
    recovered = executor.read_file(result['spill_path'])
    assert recovered['ok'] is True


def test_scenario_failed_spill_keeps_hard_cap_error(sandbox, monkeypatch):
    """Given the spill write fails, when a tool returns a payload beyond the
    hard cap, then the safety net of the original incident still applies: an
    explicit ok=False size error and no payload in the model context."""
    big_payload = 'x' * 1_650_000
    executor = ToolExecutor()
    monkeypatch.setattr(
        'dev_agent.agent_loop._spill_tool_result_doc', lambda doc, name="": None
    )

    def _fake_list_files(subdir="", max_depth=1):
        return {'ok': True, 'subdir': subdir or '.', 'content': big_payload}

    monkeypatch.setattr(executor, 'list_files', _fake_list_files)

    result = executor.dispatch('list_files', {'subdir': '.'})

    assert result['ok'] is False
    assert result['result_too_large'] is True
    assert result['result_size'] > 200_000
    assert 'NOT passed to the model' in result['error']
    assert 'content' not in result
    assert big_payload not in str(result)


def test_scenario_long_history_is_trimmed_before_request(monkeypatch):
    """Given a conversation history far above the window, when send_request
    runs, then the oldest messages are dropped before _do_request and the
    provider receives a trimmed history."""
    monkeypatch.setattr(
        'core.context_guard.get_model_context_window', lambda a, s: 1000
    )

    hist = [
        {'role': 'user', 'content': 'ancient ' * 400},
        {'role': 'assistant', 'content': 'ancient-reply ' * 400},
        {'role': 'user', 'content': 'recent question'},
    ]

    called = {}

    def _fake_do_request(**kwargs):
        called['kwargs'] = kwargs
        return 'ok'

    monkeypatch.setattr(api_layer, '_do_request', _fake_do_request)
    monkeypatch.setattr(api_layer, 'get_services', lambda: {'svc': {
        'auth_type': 'bearer',
        'base_url': 'https://example.test',
        'models': [{'id': 'm', 'context_window': 1000}],
    }})
    monkeypatch.setattr(api_layer, 'load_config', lambda: {})
    monkeypatch.setattr(api_layer, '_get_model_max_tokens', lambda *a, **k: 200)

    assistant = {'service': 'svc', 'model': 'm', 'text': 'system'}
    out = api_layer.send_request('recent question', assistant, history=hist)

    assert out == 'ok'
    sent_history = called['kwargs']['hist_msgs']
    # The ancient messages were dropped; the recent one survived.
    assert 'ancient' not in ' '.join(m['content'] for m in sent_history)
    assert sent_history[-1]['content'] == 'recent question'
    assert len(sent_history) < len(hist)


def test_scenario_impossible_payload_raises_clear_error(monkeypatch):
    """Given a single message that cannot fit the window on its own, when
    send_request runs, then ContextWindowError is raised with a
    human-readable message and NO provider request is attempted."""
    monkeypatch.setattr(
        'core.context_guard.get_model_context_window', lambda a, s: 100
    )

    called = {}

    def _fake_do_request(**kwargs):
        called['kwargs'] = kwargs
        return 'ok'

    monkeypatch.setattr(api_layer, '_do_request', _fake_do_request)
    monkeypatch.setattr(api_layer, 'get_services', lambda: {'svc': {
        'auth_type': 'bearer',
        'base_url': 'https://example.test',
        'models': [{'id': 'm', 'context_window': 100}],
    }})
    monkeypatch.setattr(api_layer, 'load_config', lambda: {})
    monkeypatch.setattr(api_layer, '_get_model_max_tokens', lambda *a, **k: 50)

    assistant = {'service': 'svc', 'model': 'm', 'text': 's' * 3000}

    with pytest.raises(ContextWindowError) as exc_info:
        api_layer.send_request('hello', assistant, history=[])

    assert exc_info.value.code == 'context_window_exceeded'
    assert 'Reduce the size of the message' in exc_info.value.message
    # The provider never received a request.
    assert called == {}
