"""Regression tests for core.naming_runtime language resolution.

A background worker (the subagent fan-out pool, a transport call, a test
process) owns no Streamlit script run. The module must resolve the language
there from the stored setting without touching ``st.session_state``: every
such touch made Streamlit log a missing-ScriptRunContext warning for each
child agent launched.
"""
import logging
import sys
import threading

import pytest

import core.naming_runtime as naming_runtime


class _Collector(logging.Handler):
    """Record every record of the watched loggers."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def streamlit_log():
    """Capture Streamlit's script-context and session-state warnings."""
    names = ("streamlit.runtime.scriptrunner_utils.script_run_context",
             "streamlit.runtime.scriptrunner",
             "streamlit.runtime.state.session_state_proxy")
    collector = _Collector()
    loggers = [logging.getLogger(name) for name in names]
    levels = []
    for logger in loggers:
        levels.append((logger, logger.level))
        logger.addHandler(collector)
        logger.setLevel(logging.DEBUG)
    try:
        yield collector
    finally:
        for logger, level in levels:
            logger.removeHandler(collector)
            logger.setLevel(level)


def _run_in_worker(func):
    """Call *func* on a fresh thread and return its result."""
    result = {}

    def target():
        result["value"] = func()

    worker = threading.Thread(target=target, name="ThreadPoolExecutor-99_0")
    worker.start()
    worker.join()
    return result["value"]


def test_worker_thread_does_not_touch_session_state(monkeypatch, streamlit_log):
    """A worker resolves the stored language and logs no context warning."""
    monkeypatch.setattr(naming_runtime, "_stored_lang", lambda: "Русский")

    touched = []
    real_session_lang = naming_runtime._session_lang

    def spy():
        touched.append(threading.current_thread().name)
        return real_session_lang()

    monkeypatch.setattr(naming_runtime, "_session_lang", spy)
    lang = _run_in_worker(naming_runtime.current_lang)

    assert lang == "Русский"
    assert touched == ["ThreadPoolExecutor-99_0"]
    assert streamlit_log.messages == []


def test_worker_thread_reads_no_session_when_context_missing(monkeypatch):
    """With no script context the session branch is skipped entirely."""
    monkeypatch.setattr(naming_runtime, "_running_script_context", lambda: None)
    monkeypatch.setattr(naming_runtime, "_stored_lang", lambda: "English")

    import streamlit as st
    reads = []

    class _State:
        def get(self, key, default=None):
            reads.append(key)
            return default

    monkeypatch.setattr(st, "session_state", _State())
    lang = _run_in_worker(naming_runtime.current_lang)

    assert lang == "English"
    assert reads == []


def test_session_language_wins_inside_a_script_run(monkeypatch):
    """A live script context still makes the session language win."""
    monkeypatch.setattr(naming_runtime, "_running_script_context", lambda: object())
    monkeypatch.setattr(naming_runtime, "_stored_lang", lambda: "English")

    import streamlit as st

    class _State:
        def get(self, key, default=None):
            assert key == "ui_lang"
            return "中文"

    monkeypatch.setattr(st, "session_state", _State())
    assert naming_runtime.current_lang() == "中文"


def test_missing_streamlit_falls_back_to_stored_language(monkeypatch):
    """An environment without Streamlit still resolves the stored setting."""
    monkeypatch.setitem(sys.modules, "streamlit", None)
    monkeypatch.setattr(naming_runtime, "_stored_lang", lambda: "Русский")
    assert _run_in_worker(naming_runtime.current_lang) == "Русский"


def test_shipped_default_is_the_last_resort(monkeypatch):
    """Without a session, a setting or a default the answer is empty."""
    monkeypatch.setattr(naming_runtime, "_running_script_context", lambda: None)
    monkeypatch.setattr(naming_runtime, "_stored_lang", lambda: "")
    monkeypatch.setattr("core.config.get_default_ui_lang", lambda: "")
    assert naming_runtime.current_lang() == ""


def test_running_script_context_never_warns(streamlit_log):
    """The context probe itself stays silent outside a script run."""
    assert naming_runtime._running_script_context() is None
    assert [m for m in streamlit_log.messages if "ScriptRunContext" in m] == []
