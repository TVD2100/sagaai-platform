# -*- coding: utf-8 -*-
"""Connection-resilience tests for the agent loop (Step 3)."""

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


def test_user_message_persisted_before_llm_call_on_failure(monkeypatch):
    """The user message must already be in history when the LLM call raises."""
    from core.api_errors import NetworkError

    def fail_send(*a, **k):
        raise NetworkError('connection reset', service='test')

    _replace_send_request(monkeypatch, fail_send)
    dispatcher = _FakeDispatcher()
    state = _make_state('потеряется ли это сообщение?')
    events = []

    state = step_agent_loop(state, dispatcher=dispatcher, on_event=events.append)

    assert state.phase == 'error'
    user_msgs = [
        m for m in state.history
        if m.get('role') == 'user' and m.get('content') == 'потеряется ли это сообщение?'
    ]
    assert len(user_msgs) == 1


def test_user_message_not_duplicated_after_success(monkeypatch):
    """After a successful LLM call the user entry must appear exactly once."""

    def ok_send(*a, **k):
        return _DONE + 'Ответ готов.'

    _replace_send_request(monkeypatch, ok_send)
    dispatcher = _FakeDispatcher()
    state = _make_state('обычное сообщение')
    events = []

    state = _run_until_terminal(state, dispatcher, events)

    user_msgs = [
        m for m in state.history
        if m.get('role') == 'user' and m.get('content') == 'обычное сообщение'
    ]
    assert len(user_msgs) == 1
    assistant_msgs = [m for m in state.history if m.get('role') == 'assistant']
    assert assistant_msgs


def test_retrying_llm_event_emitted_on_retry(monkeypatch):
    """API-layer retries must surface as retrying_llm events to on_event."""

    def retrying_send(user_message, assistant, **kwargs):
        callback = kwargs.get('retry_callback')
        assert callback is not None
        callback({'attempt': 1, 'attempts': 3, 'delay': 30.0, 'error': 'connection reset'})
        callback({'attempt': 2, 'attempts': 3, 'delay': 30.0, 'error': 'connection reset'})
        return _DONE + 'Связь восстановилась.'

    _replace_send_request(monkeypatch, retrying_send)
    dispatcher = _FakeDispatcher()
    state = _make_state('проверка ретраев')
    events = []

    state = _run_until_terminal(state, dispatcher, events)

    retries = [e for e in events if e.get('type') == 'retrying_llm']
    assert len(retries) == 2
    assert retries[0]['attempt'] == 1
    assert retries[0]['attempts'] == 3
    assert retries[0]['delay'] == 30.0
    assert retries[1]['attempt'] == 2


def test_hidden_tool_result_stays_hidden_when_persisted_early(monkeypatch):
    """Tool-result payloads must keep the hidden marker when persisted."""

    def ok_send(*a, **k):
        return _DONE + 'Инструменты выполнены.'

    _replace_send_request(monkeypatch, ok_send)
    dispatcher = _FakeDispatcher()
    state = _make_state(json.dumps({'tool_result': {'ok': True}}))
    events = []

    state = _run_until_terminal(state, dispatcher, events)

    tool_msgs = [
        m for m in state.history
        if m.get('role') == 'user' and 'tool_result' in str(m.get('content'))
    ]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].get('hidden') is True
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
