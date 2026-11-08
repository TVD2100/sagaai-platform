# -*- coding: utf-8 -*-
"""tests/test_chat_key_gate_resolution.py - does the chat page let an agent run.

An employee installed from a catalog may ship no model keys at all: the run
supplies the platform basis. The page must ask the same question the run
answers, otherwise it blocks an employee that would work.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _header(monkeypatch, orch, has_key):
    """Call the header helper with mocked streamlit and key lookup.

    Returns ``(shown, warnings)``: whether the caller may keep rendering and
    how many warnings the page emitted.
    """
    import ui.pages.orchestrator_chat as chat_page

    warnings = []

    class _Stub:
        @staticmethod
        def caption(*args, **kwargs):
            pass

        @staticmethod
        def markdown(*args, **kwargs):
            pass

        @staticmethod
        def warning(*args, **kwargs):
            warnings.append(args)

    monkeypatch.setattr(chat_page, "st", _Stub)
    monkeypatch.setattr(chat_page, "t", lambda key, **kwargs: key)
    monkeypatch.setattr(chat_page, "_assistant_has_api_key", has_key)

    shown = chat_page._render_chat_header(orch, "Name", "English")
    return shown, warnings


def test_a_config_without_model_keys_still_opens_the_page(monkeypatch):
    """The reported defect: the package ships no model keys, the page blocks."""
    seen = []

    def _has_key(service):
        seen.append(service)
        return True

    orch = {"config": {"max_steps": 5, "chat_web_search": False}}

    shown, warnings = _header(monkeypatch, orch, _has_key)

    assert shown is True
    assert warnings == []
    # The platform basis names the provider the run would use.
    assert seen == ["DeepSeek"]


def test_a_legacy_config_is_checked_against_its_own_provider(monkeypatch):
    seen = []

    def _has_key(service):
        seen.append(service)
        return True

    orch = {"config": {"service": "YandexAI", "model": "yandexgpt"}}

    shown, warnings = _header(monkeypatch, orch, _has_key)

    assert shown is True
    assert warnings == []
    assert seen == ["YandexAI"]


def test_a_missing_key_still_blocks_the_page(monkeypatch):
    orch = {"config": {"strong_service": "DeepSeek", "strong_model": "deepseek-flash"}}

    shown, warnings = _header(monkeypatch, orch, lambda service: False)

    assert shown is False
    assert len(warnings) == 1


def test_an_employee_with_no_config_at_all_is_checked_once(monkeypatch):
    """A malformed record must not crash the header."""
    called = []

    def _has_key(service):
        called.append(service)
        return True

    shown, warnings = _header(monkeypatch, {"config": None}, _has_key)

    assert shown is True
    assert warnings == []
    assert called == ["DeepSeek"]


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
