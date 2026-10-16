# -*- coding: utf-8 -*-
"""tests/scenarios/test_inline_chat_upload_scenario.py - user-level scenarios
for the inline file upload built into the orchestrator chat input.

The bulky separate ``st.file_uploader`` dropzone was replaced by
``st.chat_input(accept_file="multiple")``: the paperclip lives inside the
input field and files travel with the text submission. These scenarios walk
the send path of ``ui.pages.orchestrator._render_chat_tab`` as a user would.

Scenario 1 - the user sends text and a file together: the RAW BYTES land in
              history/<tid>/files and the thread-files notice reaches the
              agent context; the task line is the typed text.

Scenario 2 - the user sends a file with no text at all: the submission still
              reaches the agent, with the i18n placeholder naming the file.

Scenario 3 - the user sends a plain text message: no thread-file records are
              created and the task line is the typed text.

Scenario 4 - the user attaches a file over the raw-byte cap: an error is
              shown and the oversized file is NOT persisted.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture()
def ui_env(monkeypatch, tmp_path):
    """Isolated DATA_DIR + fresh ui.* modules under the streamlit mock."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    for m in list(sys.modules):
        if m == "ui" or m.startswith("ui."):
            sys.modules.pop(m, None)
    with install_streamlit_mock() as st:
        yield st


class _FakeUpload:
    """Minimal upload stand-in exposing .name and .read()."""

    def __init__(self, name, data=b"file data"):
        self.name = name
        self._data = data

    def read(self):
        return self._data


class _FakePrompt:
    """Stand-in for the ChatInputValue returned by st.chat_input."""

    def __init__(self, text="", files=None):
        self.text = text
        self.files = list(files or [])


def _chat_env(ui_env, monkeypatch, tid):
    """Seed session state and patch dependencies for _render_chat_tab."""
    import ui.pages.orchestrator as orch_page

    orch = {"slug": "o1", "name": "DevAgent", "description": "",
            "is_builtin": True, "prompt_text": "",
            "config": {"strong_service": "Svc", "strong_model": "m1"}}
    monkeypatch.setattr(orch_page, "get_orchestrator", lambda slug: orch)
    monkeypatch.setattr(orch_page, "_assistant_has_api_key", lambda svc: True)
    monkeypatch.setattr(orch_page, "t", lambda key, *a, **k: key)
    monkeypatch.setattr(
        orch_page, "build_assistant_dicts",
        lambda slug: ({"service": "Svc", "model": "m1", "temperature": 0.5,
                       "text": "p", "max_tokens": 0}, {}),
    )
    monkeypatch.setattr(
        orch_page, "check_context",
        lambda *a, **k: {"total_tokens": 10, "ok": True,
                         "limit": 1000, "excess_chars": 0},
    )
    monkeypatch.setattr(orch_page, "sum_thread_tokens", lambda msgs: (0, 0, 0))
    monkeypatch.setattr(orch_page, "_do_step", lambda **k: None)
    monkeypatch.setattr(orch_page, "_render_token_line", lambda *a, **k: None)

    ui_env.session_state.update({
        "ui_lang": "English",
        "orch_o1_history": [],
        "orch_o1_loop_state": None,
        "orch_o1_thread_id": tid,
        "orch_o1_web_search": False,
        "orch_o1_economy_mode": False,
        "orch_o1_safety_mode": True,
        "orch_o1_saved_msg_count": 0,
        "orch_o1_dispatcher": None,
        "orch_o1_scroll_bottom": False,
    })
    return orch_page


def _seed_submission(ui_env, text="", files=None):
    """Seed the mocked st.chat_input result (the widget carries no key)."""
    ui_env._chat_returns[None] = _FakePrompt(text, files)


def _send(ui_env, orch_page):
    try:
        orch_page._render_chat_tab("o1", "English")
    except StopRerun:
        pass


# ── Scenario 1 ─────────────────────────────────────────────────────────────

def test_scenario_text_and_file_sent_together(ui_env, monkeypatch):
    """Given a dialog, when the user types a message and attaches a file, then
    the raw bytes are persisted and the notice reaches the agent context."""
    from core import threads_devagent as td

    orch_page = _chat_env(ui_env, monkeypatch, "scen-inline-1")
    _seed_submission(ui_env, text="please review", files=[
        _FakeUpload("report.txt", b"hello world"),
    ])
    _send(ui_env, orch_page)

    assert ui_env.session_state["orch_o1_user_message"] == "please review"
    records = td.list_thread_files("scen-inline-1")
    assert {r["name"] for r in records} == {"report.txt"}, records
    assert td.read_thread_file("scen-inline-1", "report.txt")["content"] == "hello world"
    notice = ui_env.session_state["orch_o1_file_ctx"]
    assert "report.txt" in notice, notice


# ── Scenario 2 ─────────────────────────────────────────────────────────────

def test_scenario_file_only_submission_reaches_agent(ui_env, monkeypatch):
    """Given a dialog, when the user attaches a file without any text, then the
    submission still reaches the agent with an i18n placeholder task line."""
    from core import threads_devagent as td

    orch_page = _chat_env(ui_env, monkeypatch, "scen-inline-2")
    _seed_submission(ui_env, text="   ", files=[_FakeUpload("only.bin", b"xy")])
    _send(ui_env, orch_page)

    # t() is patched to echo the key itself in this fixture.
    assert ui_env.session_state["orch_o1_user_message"] == "orch_attach_only_message"
    assert {r["name"] for r in td.list_thread_files("scen-inline-2")} == {"only.bin"}


# ── Scenario 3 ─────────────────────────────────────────────────────────────

def test_scenario_plain_text_creates_no_files(ui_env, monkeypatch):
    """Given a dialog, when the user sends a plain text message, then no
    thread-file records appear and the task line is the typed text."""
    from core import threads_devagent as td

    orch_page = _chat_env(ui_env, monkeypatch, "scen-inline-3")
    _seed_submission(ui_env, text="just text")
    _send(ui_env, orch_page)

    assert ui_env.session_state["orch_o1_user_message"] == "just text"
    assert td.list_thread_files("scen-inline-3") == []


# ── Scenario 4 ─────────────────────────────────────────────────────────────

def test_scenario_oversized_attachment_is_rejected(ui_env, monkeypatch):
    """Given a dialog, when the user attaches a file over the raw-byte cap, then
    an error is shown and the file is NOT persisted."""
    from core import threads_devagent as td

    orch_page = _chat_env(ui_env, monkeypatch, "scen-inline-4")
    oversized = _FakeUpload("big.bin", b"A" * (orch_page.MAX_THREAD_FILE_BYTES + 1))
    _seed_submission(ui_env, text="go", files=[oversized])
    _send(ui_env, orch_page)

    assert any("orch_file_too_large_bytes" in str(e) for e in ui_env.errors), \
        ui_env.errors
    assert td.list_thread_files("scen-inline-4") == []
