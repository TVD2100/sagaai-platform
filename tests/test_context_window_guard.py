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


def test_output_reserve_formula_caps_provider_limit():
    """The output reserve is ``min(max_out, max(4096, 0.25 * window))``:
    a provider-declared limit larger than the window cannot inflate the
    fixed part, and small windows keep the 4096 floor."""
    from core.context_guard import _output_reserve

    assert _output_reserve(32768, 384000) == 8192    # DeepSeek-style 384k
    assert _output_reserve(32768, 32768) == 8192
    assert _output_reserve(1000000, 384000) == 250000
    assert _output_reserve(32768, 0) == 0            # no declared limit


def test_32k_window_does_not_raise_on_empty_history(monkeypatch):
    """A realistic 32k-window model (system prompt ~17.5k tokens, declared
    output 32768) must not raise ContextWindowError on an empty history.

    Regression: with the old half-window clamp the reserve was 16384, the
    fixed part exceeded the hard threshold and the guard rejected even an
    empty request.
    """
    monkeypatch.setattr('core.context_guard.get_model_context_window',
                        lambda a, s: 32768)
    monkeypatch.setattr('core.context_guard.estimate_tokens',
                        lambda text: max(1, len(text) // 4))
    assistant = {'text': 's' * 70000}  # ~17500 tokens
    out = apply_context_guard([], 'hello', assistant=assistant,
                              max_tokens=32768, svc_name='yandex', services={})
    assert out == []


def test_economy_meta_head_is_never_trimmed(monkeypatch):
    """The economy metadata block (the stable payload head) survives the
    guard while older history messages are dropped."""
    monkeypatch.setattr('core.context_guard.get_model_context_window',
                        lambda a, s: 1000)
    monkeypatch.setattr('core.context_guard.estimate_tokens',
                        lambda text: max(1, len(text) // 4))
    meta = {'role': 'system',
            'content': 'ECONOMY MODE: ENABLED\nCurrent workspace: x\n'
                       'Web search: disabled\n'}
    big1 = {'role': 'user', 'content': 'a' * 4000}
    big2 = {'role': 'assistant', 'content': 'b' * 4000}

    out = apply_context_guard([meta, big1, big2], 'hello',
                              assistant={'text': 'sys'}, max_tokens=0)
    assert out == [meta]  # both oversized turns trimmed, the head kept
    # A regular first message (no economy head) is still trimmable.
    assert apply_context_guard([big1], 'hello', assistant={'text': 'sys'}) == []


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


def test_history_cap_exposes_the_guard_history_budget():
    """B3: ``history_cap`` = soft threshold minus reserve minus the prompt
    estimate - the guard's effective history budget for caller-side
    alignment (the economy window keeps its budget at or below it)."""
    from core.context_guard import history_cap

    assert history_cap(36_098, 1_000, 1) == 18_049 - 1_000 - 1
    assert history_cap(32_768, 32_768) == 16_384 - 8_192
    assert history_cap(1_000_000, 384_000) == 500_000 - 250_000
    assert history_cap(1_000_000, 0) == 500_000      # no declared limit
    assert history_cap(0, 32_768) == 0               # unknown window
    assert history_cap(36_098, 1_000, -5) == 18_049 - 1_000
    assert history_cap(36_098, 1_000, 99_999) == 0   # prompt clamps to 0


def test_economy_payload_stays_below_guard_soft_threshold(monkeypatch):
    """B3: an economy-built payload fits the guard's soft threshold, so
    apply_context_guard passes it through unchanged instead of sliding the
    front message-by-message at saturation (the cache-breaking slide)."""
    import core.context_guard as cg
    from dev_agent.agent_loop import AgentLoopState, build_economy_context

    window, max_out = 1_000_000, 32_768

    def est(text):
        return max(1, len(text or "") // 4)

    monkeypatch.setattr(cg, "get_model_context_window", lambda a, s: window)
    monkeypatch.setattr("core.files.get_model_context_window", lambda a, s: window)
    monkeypatch.setattr("core.api_layer._get_model_max_tokens", lambda svc, m: max_out)
    monkeypatch.setattr(cg, "estimate_tokens", est)
    monkeypatch.setattr("core.files.estimate_tokens", est)

    prompt = "s" * 68_000  # ~17_000 tokens
    assistant = {"service": "mock", "model": "m", "text": prompt}
    big = "x" * 40_000  # ~10_000 tokens each
    state = AgentLoopState()
    state.economy_cache_enabled = True
    state.economy_cache_multiplier = 200
    state.economy_tail_messages = 5
    state.history = [{"role": "user", "content": big, "ts": "t"}
                     for _ in range(100)]

    payload = build_economy_context(state, assistant)

    # The B3-aligned budget cuts the long history and folds the cut into
    # the anchor (the legacy formula would keep far more).
    assert len(payload) < len(state.history) + 1
    assert state.economy_anchor and state.economy_anchor > 0

    # The guard passes the economy payload through: no front slide.
    out = cg.apply_context_guard(payload, "hello", assistant=assistant,
                                 max_tokens=max_out, services={})
    assert out == payload