"""
tests.test_context_window_guard - unit and integration tests for M4/M5:
core.context_guard.apply_context_guard and its wiring into send_request.
"""

import pytest

import core.api_layer as api_layer
from core.context_guard import apply_context_guard
from core.api_errors import ContextWindowError


def _calls(counter):
    records = []
    def _do_request(*args, **kwargs):
        records.append((args, kwargs))
        return 'ok'
    counter['do'] = records
    return _do_request


def test_trim_from_front_under_soft_threshold(monkeypatch):
    """Trim drops the OLDEST messages and keeps the newest in order."""
    monkeypatch.setattr('core.context_guard.get_model_context_window',
                        lambda a, s: 1000)

    hist = [{'role': 'user', 'content': 'apple ' * 200}]
    hist.append({'role': 'assistant', 'content': 'kiwi ' * 200})
    hist.append({'role': 'user', 'content': 'pear ' * 200})
    hist.append({'role': 'assistant', 'content': 'grape ' * 50})
    out = apply_context_guard(hist, 'hello', assistant={'text': 'sys ' * 100})
    # The oldest message(s) must be gone; the newest remains last.
    assert len(out) < len(hist)
    assert 'apple' not in ' '.join(m['content'] for m in out)
    assert 'kiwi' not in ' '.join(m['content'] for m in out)
    assert out[-1]['content'].startswith('grape')
    # The caller's history must stay untouched.
    assert len(hist) == 4


def test_hard_threshold_raises_context_window_error(monkeypatch):
    """Payload that cannot fit under 0.8 of the window even without history
    must raise ContextWindowError instead of hitting the provider."""
    monkeypatch.setattr('core.context_guard.get_model_context_window',
                        lambda a, s: 100)
    # The assistant attached context is huge and cannot be trimmed.
    assistant = {'text': 'system ' * 500}
    with pytest.raises(ContextWindowError) as ei:
        apply_context_guard([], 'user ' * 200, assistant=assistant)
    assert ei.value.code == 'context_window_exceeded'


def test_output_reserve_is_clamped_to_half_window(monkeypatch):
    """A provider-declared max output larger than the window itself must
    not fail the guard: the reserve is clamped to half the window."""
    monkeypatch.setattr('core.context_guard.get_model_context_window',
                        lambda a, s: 1000)
    monkeypatch.setattr('core.context_guard.estimate_tokens',
                        lambda text: max(1, int(len(text) / 10)))
    # max_tokens=384000 (DeepSeek style) cannot inflate the fixed part.
    out = apply_context_guard(
        [{'role': 'user', 'content': 'hi' * 100}],
        'hello',
        assistant={'text': 'sys' * 200},
        max_tokens=384000,
        svc_name='svc',
        services={},
    )
    assert isinstance(out, list)


def test_passthrough_when_window_unknown(monkeypatch):
    """Window <= 0 (unknown service/model info) must be a passthrough."""
    monkeypatch.setattr('core.context_guard.get_model_context_window',
                        lambda a, s: 0)
    hist = [{'role': 'user', 'content': 'hello'}]
    assert apply_context_guard(hist, 'user msg', assistant={}) is hist


def test_passthrough_when_lookup_errors(monkeypatch):
    """A lookup exception must degrade to a passthrough, never crash."""
    def _fail(a, s):
        raise RuntimeError('registry unavailable')
    monkeypatch.setattr('core.context_guard.get_model_context_window', _fail)
    hist = [{'role': 'user', 'content': 'hello'}]
    assert apply_context_guard(hist, 'user msg', assistant={}) is hist


def test_integration_send_request_guards_before_do_request(monkeypatch):
    """send_request calls the guard and passes the trimmed history into
    _do_request - and a clear ContextWindowError surfaces instead of a
    provider round-trip when the request is hopeless."""

    monkeypatch.setattr('core.context_guard.get_model_context_window',
                        lambda a, s: 1000)

    hist = [
        {'role': 'user', 'content': 'old ' * 800},
        {'role': 'assistant', 'content': 'reply ' * 20},
    ]

    called = {}
    monkeypatch.setattr(api_layer, '_do_request', _calls(called))
    monkeypatch.setattr(api_layer, 'get_services', lambda: {'svc': {
        'auth_type': 'bearer', 'base_url': 'https://example.test',
        'models': [{'id': 'm', 'context_window': 1000}],
    }})
    monkeypatch.setattr(api_layer, 'load_config', lambda: {})
    monkeypatch.setattr(api_layer, '_get_model_max_tokens', lambda *a, **k: 200)
    monkeypatch.setattr(api_layer, 'ApiKeyMissingError', type('_E', (Exception,), {}))

    assistant = {'service': 'svc', 'model': 'm', 'text': 'sys ' * 50}
    out = api_layer.send_request('hello', assistant, history=hist)
    assert out == 'ok'
    hist_passed = called['do'][0][1]['hist_msgs']
    # The huge old message was dropped by the guard before _do_request.
    assert len(hist_passed) < len(hist)


def test_integration_hard_fail_raises_before_do_request(monkeypatch):
    """When trimming cannot help, send_request raises ContextWindowError
    and _do_request is never called."""
    monkeypatch.setattr('core.context_guard.get_model_context_window',
                        lambda a, s: 100)

    called = {}
    monkeypatch.setattr(api_layer, '_do_request', _calls(called))
    monkeypatch.setattr(api_layer, 'get_services', lambda: {'svc': {
        'auth_type': 'bearer', 'base_url': 'https://example.test',
        'models': [],
    }})
    monkeypatch.setattr(api_layer, 'load_config', lambda: {})
    monkeypatch.setattr(api_layer, 'ApiKeyMissingError', type('_E', (Exception,), {}))

    assistant = {'service': 'svc', 'model': 'm', 'text': 's' * 3000}
    with pytest.raises(ContextWindowError):
        api_layer.send_request('hello', assistant, history=[])
    assert not called['do']
