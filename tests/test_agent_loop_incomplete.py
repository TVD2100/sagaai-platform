# -*- coding: utf-8 -*-
"""Truncated (incomplete) Responses recovery tests for the agent loop.

When a provider answers HTTP 200 with ``status == "incomplete"`` (typically
because the reasoning budget consumed the whole ``max_output_tokens``
allowance), the body can carry NO user-visible text. The agent loop must
retry exactly ONCE with the reasoning effort lowered to ``none`` so the
whole output budget goes to visible text, and surface an
``incomplete_retry`` event to the UI.
"""

import json

from dev_agent.agent_loop import AgentLoopState, step_agent_loop


class _FakeCore:
    """Minimal dispatcher core used by step_agent_loop."""

    def __init__(self):
        self._in_agent_loop = False

    def set_history(self, *a, **k):
        pass

    def set_send_request(self, *a, **k):
        pass


class _FakeDispatcher:
    """Dispatcher whose only active path is the LLM call."""

    def __init__(self):
        self.core = _FakeCore()

    def dispatch(self, tool, args):
        return {'ok': True, 'tool': tool, 'args': args}

    def dispatch_json(self, call):
        return {'ok': True, 'tool': call.get('tool', '')}


_DONE = json.dumps({'loop_status': 'awaiting_user'}) + ' '
_INCOMPLETE = {
    'status': 'incomplete',
    'reason': 'max_output_tokens',
    'id': 'resp_1',
    'output_tokens': 384000,
}


def _make_state(user_message):
    state = AgentLoopState(task=user_message, auto_apply=False)
    state.phase = 'calling_llm'
    state.user_message = user_message
    return state


def _replace_send_request(monkeypatch, fn):
    import dev_agent.agent_loop as loop
    monkeypatch.setattr(loop, 'send_request', fn)


def _run_until_terminal(state, dispatcher, events):
    for _ in range(10):
        state = step_agent_loop(state, dispatcher=dispatcher, on_event=events.append)
        if state.phase in ('done', 'error'):
            break
    return state


def test_incomplete_without_text_retries_once_with_lower_effort(monkeypatch):
    """An incomplete answer with no text triggers exactly ONE retry with
    reasoning_effort='none'."""
    calls = []

    def send(user_message, assistant, **kwargs):
        calls.append(dict(assistant))
        cb = kwargs.get('incomplete_callback')
        assert cb is not None, 'incomplete_callback was not wired through'
        if len(calls) == 1:
            cb(dict(_INCOMPLETE))
            return ''  # truncated body with no visible text
        return _DONE + 'Восстановленный ответ.'

    _replace_send_request(monkeypatch, send)
    dispatcher = _FakeDispatcher()
    state = _make_state('сложный вопрос с большим reasoning')
    events = []

    state = _run_until_terminal(state, dispatcher, events)

    assert len(calls) == 2, 'expected exactly one recovery retry'
    assert calls[-1].get('reasoning_effort') == 'none'
    assert 'Восстановленный ответ.' in state.assistant_text


def test_incomplete_with_text_does_not_retry(monkeypatch):
    """When the truncated body still carries visible text, it is kept as is."""
    calls = []

    def send(user_message, assistant, **kwargs):
        calls.append(dict(assistant))
        cb = kwargs.get('incomplete_callback')
        if cb:
            cb(dict(_INCOMPLETE))
        return _DONE + 'Частичный, но видимый текст.'

    _replace_send_request(monkeypatch, send)
    dispatcher = _FakeDispatcher()
    state = _make_state('вопрос')
    events = []

    state = _run_until_terminal(state, dispatcher, events)

    assert len(calls) == 1, 'a non-empty answer must not be retried'
    retries = [e for e in events if e.get('type') == 'incomplete_retry']
    assert retries == []


def test_incomplete_retry_event_emitted(monkeypatch):
    """The recovery retry must surface an incomplete_retry event with details."""
    calls = []

    def send(user_message, assistant, **kwargs):
        calls.append(dict(assistant))
        cb = kwargs.get('incomplete_callback')
        if len(calls) == 1:
            cb(dict(_INCOMPLETE))
            return ''
        return _DONE + 'Ответ после повтора.'

    _replace_send_request(monkeypatch, send)
    dispatcher = _FakeDispatcher()
    state = _make_state('вопрос')
    events = []

    state = _run_until_terminal(state, dispatcher, events)

    retries = [e for e in events if e.get('type') == 'incomplete_retry']
    assert len(retries) == 1
    assert retries[0]['reason'] == 'max_output_tokens'
    assert retries[0]['output_tokens'] == 384000


def test_recovery_retry_resends_file_context(monkeypatch):
    """The recovery retry must carry the SAME file context as the first call
    (the context is only cleared after the recovery block)."""
    calls = []

    def send(user_message, assistant, **kwargs):
        calls.append(kwargs.get('file_context'))
        cb = kwargs.get('incomplete_callback')
        if len(calls) == 1:
            cb(dict(_INCOMPLETE))
            return ''
        return _DONE + 'Ответ с файлом.'

    _replace_send_request(monkeypatch, send)
    dispatcher = _FakeDispatcher()
    state = _make_state('вопрос по файлу')
    state.file_context = 'СОДЕРЖИМОЕ ФАЙЛА'
    events = []

    state = _run_until_terminal(state, dispatcher, events)

    assert calls == ['СОДЕРЖИМОЕ ФАЙЛА', 'СОДЕРЖИМОЕ ФАЙЛА']


def test_no_incomplete_no_retry(monkeypatch):
    """A normal answer never triggers a recovery retry or event."""
    calls = []

    def send(user_message, assistant, **kwargs):
        calls.append(dict(assistant))
        return _DONE + 'Обычный ответ.'

    _replace_send_request(monkeypatch, send)
    dispatcher = _FakeDispatcher()
    state = _make_state('вопрос')
    events = []

    state = _run_until_terminal(state, dispatcher, events)

    assert len(calls) == 1
    retries = [e for e in events if e.get('type') == 'incomplete_retry']
    assert retries == []
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
