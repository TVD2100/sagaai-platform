# -*- coding: utf-8 -*-
"""tests/scenarios/test_gigachat_orchestrator_scenario.py - user-level scenario
tests for running an employee (orchestrator) on the GigaChat provider.

GigaChat accepts AT MOST ONE system message and it MUST be the first element
of ``messages`` (HTTP 422 otherwise). The orchestrator loop injects system
blocks (economy-mode metadata, external task state, thread context) into the
middle of the history, and it may append a user message right after another
user message - both shapes used to break every GigaChat dialog. The employee
config may also carry max_tokens saved for another model (384000 for
DeepSeek), which must be clamped to the GigaChat cap (32768).

Scenarios (given -> when -> then), driven through the real UI entry point
``ui.pages.orchestrator._do_step`` (one call per Streamlit rerun) with the
real ``core.api_layer.send_request`` on a mocked HTTP session:

  Scenario 1 - happy path: exactly one leading system message (employee
               prompt + economy metadata folded), max_tokens clamped, and
               the assistant reply lands in the dialog history.
  Scenario 2 - error state: the provider answers HTTP 422; the failure is
               rendered in the chat feed from the events attached to the
               user message, the loop state is cleared and the next turn
               succeeds.
  Scenario 3 - edge case: a trailing user message plus the new question must
               not produce consecutive user roles - they are merged, and the
               in-history system block is folded into the single leading
               system message.
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

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402

GIGACHAT_ENDPOINT = "https://api.giga.chat/v1/chat/completions"
EMPLOYEE_PROMPT = "You are the orchestrator."


def _gigachat_service() -> dict:
    """The real shipped GigaChat service definition."""
    return json.loads((ROOT / "services" / "gigachat.json").read_text(encoding="utf-8"))


def _ok_response(text: str = "Everything is done.") -> MagicMock:
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 6},
    }
    return resp


def _error_response(status: int = 422,
                    message: str = "system message must be the first message") -> MagicMock:
    resp = MagicMock()
    resp.ok = False
    resp.status_code = status
    resp.json.return_value = {"message": message}
    resp.text = message
    return resp


def _make_session(response: MagicMock = None) -> MagicMock:
    session = MagicMock()
    session.post.return_value = response or _ok_response()
    return session


@pytest.fixture()
def ui_env(tmp_path):
    """Streamlit mock with freshly imported ui.* modules.

    Mirrors the fixture from test_orchestrator_economy_cache: the mock must
    be installed BEFORE importing ui.pages.orchestrator, otherwise the page
    binds the real Streamlit ``st`` and session_state silently no-ops.
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
    """Route every GigaChat request of the test through *session*."""
    import core.api_layer as api_layer

    monkeypatch.setattr(api_layer, "get_services",
                        lambda: {"GigaChat": _gigachat_service()})
    monkeypatch.setattr(api_layer, "load_config", lambda: {
        "GIGACHAT_API_KEY": "creds-base64",
        "GIGACHAT_SCOPE": "GIGACHAT_API_PERS",
    })
    monkeypatch.setattr(api_layer, "load_assistant_files_context", lambda *a, **k: "")
    monkeypatch.setattr(api_layer, "_gigachat_token", lambda *a, **k: "giga-token")
    monkeypatch.setattr(api_layer.requests, "Session", lambda: session)


def _setup_page(monkeypatch):
    """Import the orchestrator page and wire the employee seams."""
    import ui.pages.orchestrator as orch_page

    strong = {"service": "GigaChat", "model": "GigaChat-3-Pro",
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
                                   "config": {"strong_service": "GigaChat",
                                              "strong_model": "GigaChat-3-Pro"},
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

def test_scenario_orchestrator_turn_on_gigachat_sends_valid_payload(monkeypatch, ui_env):
    """Given an employee running on GigaChat with economy mode ON,
    when  the user sends the first message,
    then  the provider receives a valid payload: exactly one leading system
          message (employee prompt + economy metadata folded), max_tokens
          clamped from 384000 to the GigaChat cap 32768, and the assistant
          reply lands in the dialog history."""
    orch_page = _setup_page(monkeypatch)
    session = _make_session()
    _patch_transport(monkeypatch, session)
    slug = "giga_happy"

    _seed(orch_page, ui_env, slug, history=[], user_message="Hello, GigaChat!")

    _drive(ui_env, orch_page, slug)

    assert session.post.call_count == 1
    assert session.post.call_args[0][0] == GIGACHAT_ENDPOINT
    payload = session.post.call_args[1]["json"]
    assert payload["model"] == "GigaChat-3-Pro"
    # 384000 saved in the employee config is clamped to the GigaChat cap.
    assert payload["max_tokens"] == 32768

    msgs = payload["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].startswith(EMPLOYEE_PROMPT)
    assert "ECONOMY MODE" in msgs[0]["content"]
    assert [m["role"] for m in msgs].count("system") == 1
    roles = [m["role"] for m in msgs[1:]]
    assert roles == ["user"]
    assert msgs[-1]["content"] == "Hello, GigaChat!"

    hist = ui_env.session_state[f"orch_{slug}_history"]
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert "Everything is done." in hist[-1]["content"]


# --- Scenario 2 - provider error state ---------------------------------------

def test_scenario_provider_422_is_visible_in_feed_and_next_turn_recovers(monkeypatch, ui_env):
    """Given the provider rejects the request with HTTP 422,
    when  the user sends a message,
    then  the error events are attached to the user message, the chat feed
          renders the failure (st.error) instead of silently swallowing it,
          the loop state is cleared, and the next turn goes through once
          the provider accepts the payload."""
    orch_page = _setup_page(monkeypatch)
    session = _make_session(_error_response())
    _patch_transport(monkeypatch, session)
    slug = "giga_error"

    ui_env.chat_input = lambda *a, **k: ""
    _seed(orch_page, ui_env, slug, history=[], user_message="Hello")

    _drive(ui_env, orch_page, slug)

    hist = ui_env.session_state[f"orch_{slug}_history"]
    assert [m["role"] for m in hist] == ["user"]
    err_events = [e for e in hist[0].get("_events", []) if e.get("type") == "error"]
    assert err_events, hist[0]
    assert "HTTP 422" in err_events[0]["error"]
    assert "system message must be the first message" in err_events[0]["error"]
    assert ui_env.session_state[f"orch_{slug}_loop_state"] is None

    # The chat feed shows the very same failure to the user.
    start = len(ui_env.calls)
    try:
        orch_page._render_chat_tab(slug, "English")
    except StopRerun:
        pass
    errors = [call[1][0] for call in ui_env.calls[start:] if call[0] == "error"]
    assert any("HTTP 422" in str(e) for e in errors), errors

    # The next turn goes through normally.
    session.post.return_value = _ok_response("Recovered reply.")
    ui_env.session_state[f"orch_{slug}_user_message"] = "Try again"
    _drive(ui_env, orch_page, slug)

    hist = ui_env.session_state[f"orch_{slug}_history"]
    assert [m["role"] for m in hist] == ["user", "user", "assistant"]
    assert "Recovered reply." in hist[-1]["content"]
    payload = session.post.call_args[1]["json"]
    assert [m["role"] for m in payload["messages"]].count("system") == 1


# --- Scenario 3 - consecutive roles + economy meta ---------------------------

def test_scenario_economy_meta_and_consecutive_user_roles_are_folded(monkeypatch, ui_env):
    """Given a dialog whose last entry is a user message (e.g. after a failed
    turn) so the new question would produce two consecutive user roles,
    when  the turn runs in economy mode,
    then  the roles are merged with a blank line, the economy metadata is
          folded into the single leading system message, and no UI-only
          keys reach the wire."""
    orch_page = _setup_page(monkeypatch)
    session = _make_session()
    _patch_transport(monkeypatch, session)
    slug = "giga_edge"

    history = [
        {"role": "user", "content": "First question", "ts": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "First answer", "ts": "2026-01-01T00:00:01"},
        {"role": "user", "content": "one more detail", "ts": "2026-01-01T00:00:02"},
    ]
    _seed(orch_page, ui_env, slug, history=history, user_message="Second question")

    _drive(ui_env, orch_page, slug)

    payload = session.post.call_args[1]["json"]
    msgs = payload["messages"]
    assert msgs[0]["role"] == "system"
    assert EMPLOYEE_PROMPT in msgs[0]["content"]
    assert "ECONOMY MODE" in msgs[0]["content"]
    assert [m["role"] for m in msgs].count("system") == 1

    roles = [m["role"] for m in msgs[1:]]
    assert roles == ["user", "assistant", "user"]
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))
    assert "one more detail" in msgs[-1]["content"]
    assert msgs[-1]["content"].endswith("Second question")

    assert payload["max_tokens"] == 32768
    # UI-only keys (ts/_index/_events/hidden) never reach the provider.
    assert all(set(m) == {"role", "content"} for m in msgs)
