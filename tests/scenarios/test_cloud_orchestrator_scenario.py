# -*- coding: utf-8 -*-
"""tests/scenarios/test_cloud_orchestrator_scenario.py - user-level scenario
tests for running an employee (orchestrator) on the Cloud provider.

Cloud is a plain OpenAI-compatible gateway: the transport assembles
``[system prompt] + history + [question]`` and posts it with a bearer key.
The employee config may carry max_tokens saved for another model (384000),
which must be clamped to the Cloud cap (32768).

Scenarios (given -> when -> then), driven through the real UI entry point
``ui.pages.orchestrator._do_step`` (one call per Streamlit rerun) with the
real ``core.api_layer.send_request`` on a mocked HTTP transport:

  Scenario 1 - happy path: the employee prompt opens the request as the
               leading system message, the economy block rides along in the
               history, max_tokens is clamped, and the assistant reply lands
               in the dialog history.
  Scenario 2 - error state: the provider rejects the key; the failure is
               rendered in the chat feed from the events attached to the
               user message, the loop state is cleared and the next turn
               succeeds.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests._st_mock import StopRerun, install_streamlit_mock  # noqa: E402

CLOUD_ENDPOINT = "https://foundation-models.api.cloud.ru/v1/chat/completions"
CLOUD_MODEL = "GigaChat/GigaChat-4-708B-A41"
EMPLOYEE_PROMPT = "You are the orchestrator."


def _cloud_service() -> dict:
    """The real shipped Cloud service definition."""
    return json.loads((ROOT / "services" / "cloud.json").read_text(encoding="utf-8"))


def _ok_response(text: str = "Everything is done.") -> MagicMock:
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 6},
    }
    return resp


def _error_response(status: int = 401,
                    message: str = "Unauthorized") -> MagicMock:
    resp = MagicMock()
    resp.ok = False
    resp.status_code = status
    resp.json.return_value = {"error": {"message": message}}
    resp.text = message
    return resp


def _make_session(response: MagicMock = None) -> MagicMock:
    session = MagicMock()
    session.post.return_value = response or _ok_response()
    return session


@pytest.fixture()
def ui_env(tmp_path):
    """Streamlit mock with freshly imported ui.* modules.

    The mock must be installed BEFORE importing ui.pages.orchestrator,
    otherwise the page binds the real Streamlit ``st`` and session_state
    silently no-ops.
    """
    import os

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["SAGAAI_DATA_DIR"] = str(data_dir)

    saved_ui = {}
    for m in list(sys.modules):
        if m == "ui" or m.startswith("ui."):
            saved_ui[m] = sys.modules.pop(m, None)

    with install_streamlit_mock() as st:
        try:
            yield st
        finally:
            for m, mod in saved_ui.items():
                if mod is not None:
                    sys.modules[m] = mod
                else:
                    sys.modules.pop(m, None)


def _patch_transport(monkeypatch, session: MagicMock) -> None:
    """Route every Cloud request of the test through *session*."""
    import core.api_layer as api_layer

    monkeypatch.setattr(api_layer, "get_services",
                        lambda: {"Cloud": _cloud_service()})
    monkeypatch.setattr(api_layer, "load_config", lambda: {
        "CLOUD_API_KEY": "cloud-key",
    })
    monkeypatch.setattr(api_layer, "load_assistant_files_context", lambda *a, **k: "")
    monkeypatch.setattr(api_layer.requests, "post", session.post)


def _setup_page(monkeypatch):
    """Import the orchestrator page and wire the employee seams."""
    import dev_agent.agent_loop as al
    import ui.pages.orchestrator as orch_page

    # Context snapshots are covered by test_agent_loop_snapshot_chain.py;
    # neutralize the ambient runtime sources so the scenario expectations
    # (history roles / wire payload) stay deterministic.
    monkeypatch.setattr(al, "_maybe_task_state_context", lambda: None)
    monkeypatch.setattr(al, "_maybe_thread_context", lambda state: None)

    strong = {"service": "Cloud", "model": CLOUD_MODEL,
              "temperature": 0.3, "text": EMPLOYEE_PROMPT, "max_tokens": 384000}

    class _Core:
        _safety_enabled = True

        def set_history(self, *a, **k):
            pass

        def set_send_request(self, *a, **k):
            pass

    class _Dispatcher:
        def __init__(self):
            self.core = _Core()

        def dispatch(self, tool, args):
            return {"ok": True}

    monkeypatch.setattr(orch_page, "_make_dispatcher", lambda s: _Dispatcher())
    monkeypatch.setattr(orch_page, "_make_send_adapter",
                        lambda lang, slug: (lambda *a, **k: ""))
    monkeypatch.setattr(orch_page, "get_orchestrator",
                        lambda s: {"name": "DevAgent", "description": "",
                                   "config": {"strong_service": "Cloud",
                                              "strong_model": CLOUD_MODEL},
                                   "max_steps": 10})
    monkeypatch.setattr(orch_page, "build_assistant_dicts", lambda s: (strong, strong))
    monkeypatch.setattr(orch_page, "get_economy_config", lambda s: {})
    monkeypatch.setattr(orch_page, "_assistant_has_api_key", lambda svc: True)
    monkeypatch.setattr(orch_page, "check_context",
                        lambda *a, **k: {"total_tokens": 10, "ok": True,
                                         "limit": 1000, "excess_chars": 0})
    monkeypatch.setattr(orch_page, "sum_thread_tokens", lambda msgs: (0, 0, 0))
    return orch_page


def _seed(orch_page, st, slug: str, history: list, user_message: str) -> None:
    """Fresh session state with the given dialog and a pending user message."""
    orch_page._init_orch_state(slug)
    st.session_state[f"orch_{slug}_economy_mode"] = True
    st.session_state[f"orch_{slug}_web_search"] = False
    st.session_state[f"orch_{slug}_safety_mode"] = True
    st.session_state[f"orch_{slug}_history"] = list(history)
    st.session_state[f"orch_{slug}_user_message"] = user_message


def _drive(st, orch_page, slug: str, limit: int = 30) -> None:
    """Call _do_step until the loop waits for the user or terminates."""
    for _ in range(limit):
        orch_page._do_step(slug, "English")
        ls = st.session_state.get(f"orch_{slug}_loop_state")
        if ls is None:
            return
        if getattr(ls, "final_status", None) in (
            "awaiting_user", "awaiting_approval",
            "awaiting_confirmation", "sanitized_required",
        ):
            return
    raise AssertionError("agent loop did not reach a terminal state")


# --- Scenario 1 - happy path -------------------------------------------------

def test_scenario_orchestrator_turn_on_cloud_sends_valid_payload(monkeypatch, ui_env):
    """Given an employee running on Cloud with economy mode ON,
    when  the user sends the first message,
    then  the request goes to the Cloud endpoint with a bearer header, the
          employee prompt opens the messages, max_tokens is clamped from
          384000 to the Cloud cap 32768, and the reply lands in history."""
    orch_page = _setup_page(monkeypatch)
    session = _make_session()
    _patch_transport(monkeypatch, session)
    slug = "cloud_happy"

    _seed(orch_page, ui_env, slug, history=[], user_message="Hello, Cloud!")

    _drive(ui_env, orch_page, slug)

    assert session.post.call_count == 1
    assert session.post.call_args[0][0] == CLOUD_ENDPOINT
    headers = session.post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer cloud-key"
    payload = session.post.call_args[1]["json"]
    assert payload["model"] == CLOUD_MODEL
    # 384000 saved in the employee config is clamped to the Cloud cap.
    assert payload["max_tokens"] == 32768

    msgs = payload["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].startswith(EMPLOYEE_PROMPT)
    assert any("ECONOMY MODE" in (m.get("content") or "") for m in msgs)
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"] == "Hello, Cloud!"
    # UI-only keys (ts/_index/_events/hidden) never reach the provider.
    assert all(set(m) == {"role", "content"} for m in msgs)

    hist = ui_env.session_state[f"orch_{slug}_history"]
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert "Everything is done." in hist[-1]["content"]


# --- Scenario 2 - provider error state ---------------------------------------

def test_scenario_provider_error_is_visible_in_feed_and_next_turn_recovers(monkeypatch, ui_env):
    """Given the provider rejects the request with HTTP 401,
    when  the user sends a message,
    then  the error events are attached to the user message, the chat feed
          renders the failure (st.error) instead of silently swallowing it,
          the loop state is cleared, and the next turn goes through once
          the provider accepts the payload."""
    orch_page = _setup_page(monkeypatch)
    session = _make_session(_error_response())
    _patch_transport(monkeypatch, session)
    slug = "cloud_error"

    ui_env.chat_input = lambda *a, **k: ""
    _seed(orch_page, ui_env, slug, history=[], user_message="Hello")

    _drive(ui_env, orch_page, slug)

    hist = ui_env.session_state[f"orch_{slug}_history"]
    assert [m["role"] for m in hist] == ["user"]
    err_events = [e for e in hist[0].get("_events", []) if e.get("type") == "error"]
    assert err_events, hist[0]
    assert "401" in err_events[0]["error"]
    assert ui_env.session_state[f"orch_{slug}_loop_state"] is None

    # The chat feed shows the very same failure to the user.
    start = len(ui_env.calls)
    try:
        orch_page._render_chat_tab(slug, "English")
    except StopRerun:
        pass
    errors = [call[1][0] for call in ui_env.calls[start:] if call[0] == "error"]
    assert any("401" in str(e) for e in errors), errors

    # The next turn goes through normally.
    session.post.return_value = _ok_response("Recovered reply.")
    ui_env.session_state[f"orch_{slug}_user_message"] = "Try again"
    _drive(ui_env, orch_page, slug)

    hist = ui_env.session_state[f"orch_{slug}_history"]
    assert [m["role"] for m in hist] == ["user", "user", "assistant"]
    assert "Recovered reply." in hist[-1]["content"]

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
