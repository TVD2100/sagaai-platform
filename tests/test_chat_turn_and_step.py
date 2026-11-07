# -*- coding: utf-8 -*-
"""Tests for the two turn layers the pages delegate to.

``core.chat_turn`` owns one assistant exchange: the history that reaches the
model, the call itself and the stored pair of messages. ``core.orchestrator_step``
owns the write of a finished agent step: which messages reach the thread and
how the saved cursor moves.

Both are checked against fakes of the store and the provider, never against a
real database or network: the point of the extraction is that the rule can be
tested without a screen and without a provider.
"""
from unittest.mock import patch

import pytest

from core import chat_turn, orchestrator_step

# ── chat_turn: the model call ────────────────────────────────────────────────


def test_a_new_dialog_sends_no_history():
    """There is no thread, so nothing can be resent."""
    assert chat_turn.turn_history(None) == []
    assert chat_turn.turn_history("") == []


def test_the_history_of_a_thread_reaches_the_model_in_api_form():
    """The stored messages are converted, not passed through raw."""
    stored = [{"role": "user", "content": "hi", "_events": [{"type": "x"}]}]
    converted = [{"role": "user", "content": "hi"}]
    with patch.object(chat_turn, "load_thread_messages", return_value=stored) as load, \
         patch.object(chat_turn, "messages_to_api_history", return_value=converted) as to_api:
        assert chat_turn.turn_history("tid-1") == converted
    load.assert_called_once_with("tid-1")
    to_api.assert_called_once_with(stored)


def test_the_turn_passes_every_option_to_the_provider():
    """Language, file context and the usage callback are not dropped."""
    callback = object()
    with patch.object(chat_turn, "send_request", return_value="answer") as send:
        result = chat_turn.run_chat_turn(
            "question", {"model": "m"}, "file text",
            history=[{"role": "user", "content": "old"}],
            lang="ru", usage_callback=callback,
        )
    assert result == "answer"
    send.assert_called_once_with(
        "question", {"model": "m"}, "file text",
        history=[{"role": "user", "content": "old"}], lang="ru",
        usage_callback=callback,
    )


def test_the_turn_without_history_sends_an_empty_list():
    """The provider must never receive ``None`` where it iterates."""
    with patch.object(chat_turn, "send_request", return_value="a") as send:
        chat_turn.run_chat_turn("q", {})
    assert send.call_args.kwargs["history"] == []


def test_a_provider_failure_is_raised_to_the_caller():
    """The page decides how to show a failure; this layer does not swallow it."""
    with patch.object(chat_turn, "send_request", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            chat_turn.run_chat_turn("q", {})


# ── chat_turn: the usage figures ─────────────────────────────────────────────


def test_no_reported_usage_stays_none():
    """A provider that reports nothing differs from one reporting zeros."""
    _callback, read = chat_turn.collect_tokens()
    assert read() is None


def test_the_reported_usage_is_normalised():
    """All three figures are kept; a missing cache counts as zero."""
    callback, read = chat_turn.collect_tokens()
    callback({"in": 10, "out": 4})
    assert read() == {"in": 10, "out": 4, "cache": 0}


def test_a_second_report_replaces_the_first():
    """The callback fires once per model call; the last call is the turn's."""
    callback, read = chat_turn.collect_tokens()
    callback({"in": 1, "out": 1, "cache": 0})
    callback({"in": 7, "out": 3, "cache": 2})
    assert read() == {"in": 7, "out": 3, "cache": 2}


def test_the_reader_returns_a_copy():
    """The caller must not be able to mutate the collector's state."""
    callback, read = chat_turn.collect_tokens()
    callback({"in": 1, "out": 2})
    figures = read()
    figures["in"] = 999
    assert read()["in"] == 1


# ── chat_turn: the stored exchange ───────────────────────────────────────────


def test_an_existing_thread_is_reused():
    """A follow-up message must not start a second dialog."""
    with patch.object(chat_turn, "create_thread") as create, \
         patch.object(chat_turn, "append_thread_message") as append, \
         patch.object(chat_turn, "save_thread_file"):
        tid = chat_turn.persist_chat_turn("tid-existing", "a1", "Name", "q", "a")
    assert tid == "tid-existing"
    create.assert_not_called()
    assert append.call_count == 2


def test_a_new_thread_is_created_before_anything_is_written():
    """The file row and the messages need a thread to point at."""
    calls = []
    with patch.object(chat_turn, "create_thread", side_effect=lambda *a: calls.append("create") or "tid-new"), \
         patch.object(chat_turn, "save_thread_file", side_effect=lambda *a: calls.append("file")), \
         patch.object(chat_turn, "append_thread_message", side_effect=lambda *a, **k: calls.append("msg")):
        tid = chat_turn.persist_chat_turn(None, "a1", "Name", "q", "a",
                                          file_name="f.txt", file_context="text",
                                          tokens={"in": 1})
    assert tid == "tid-new"
    assert calls == ["create", "file", "msg", "msg"]


def test_the_user_message_is_written_before_the_answer():
    """A reader must never see a reply without its request."""
    rows = []
    with patch.object(chat_turn, "append_thread_message",
                      side_effect=lambda *a, **k: rows.append((a[1], a[2], k))):
        chat_turn.persist_chat_turn("tid", "a1", "N", "question", "answer",
                                    file_name="f.txt", file_context="abc",
                                    tokens={"in": 5, "out": 2})
    assert [row[0] for row in rows] == ["user", "assistant"]
    assert rows[0][1] == "question"
    assert rows[0][2] == {"file_name": "f.txt", "file_chars": 3}
    assert rows[1][1] == "answer"
    assert rows[1][2] == {"tokens": {"in": 5, "out": 2}}


def test_a_file_without_text_is_not_stored():
    """Only a real upload becomes a file row; the name alone proves nothing."""
    with patch.object(chat_turn, "save_thread_file") as save, \
         patch.object(chat_turn, "append_thread_message"):
        chat_turn.persist_chat_turn("tid", "a1", "N", "q", "a", file_name="f.txt", file_context="")
    save.assert_not_called()


def test_a_file_without_a_name_is_not_stored():
    """The mirror case: text without a name has nothing to show in the feed."""
    with patch.object(chat_turn, "save_thread_file") as save, \
         patch.object(chat_turn, "append_thread_message"):
        chat_turn.persist_chat_turn("tid", "a1", "N", "q", "a", file_name="", file_context="text")
    save.assert_not_called()


# ── orchestrator_step: the cursor ────────────────────────────────────────────


def test_only_the_unsaved_messages_are_taken():
    """The cursor cuts the history, it does not filter it."""
    history = [{"content": "1"}, {"content": "2"}, {"content": "3"}]
    assert orchestrator_step.messages_to_persist(history, 1) == history[1:]


def test_an_empty_history_yields_nothing():
    assert orchestrator_step.messages_to_persist([], 0) == []
    assert orchestrator_step.messages_to_persist(None, 3) == []


def test_a_negative_cursor_starts_from_the_beginning():
    """A reset dialog may leave a stale negative value behind."""
    history = [{"content": "1"}]
    assert orchestrator_step.messages_to_persist(history, -2) == history


def test_a_cursor_past_the_end_writes_nothing():
    """The history shrank while a step was running: do not re-append old rows."""
    history = [{"content": "1"}]
    assert orchestrator_step.messages_to_persist(history, 5) == []


# ── orchestrator_step: the row mapping ──────────────────────────────────────


def test_every_stored_attribute_is_mapped():
    """Events, usage, the file and the hidden flag travel with the message."""
    msg = {"role": "assistant", "content": "hi", "file_name": "f", "file_chars": 2,
           "_events": [{"type": "step"}], "_tokens": {"in": 1}, "hidden": 1}
    assert orchestrator_step.message_row(msg) == {
        "role": "assistant", "content": "hi", "file_name": "f", "file_chars": 2,
        "events": [{"type": "step"}], "tokens": {"in": 1}, "hidden": True,
    }


def test_a_plain_message_gets_the_store_defaults():
    """A user message carries no events and no usage."""
    row = orchestrator_step.message_row({"content": "q"})
    assert row == {"role": "user", "content": "q", "file_name": "", "file_chars": 0,
                   "events": None, "tokens": None, "hidden": False}


# ── orchestrator_step: the write ────────────────────────────────────────────


def test_the_write_appends_each_unsaved_message_once():
    """The store is called per message, with the mapped arguments."""
    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    with patch.object(orchestrator_step, "append_thread_message") as append:
        cursor = orchestrator_step.persist_step_messages("tid", history, 1)
    assert cursor == 2
    append.assert_called_once()
    assert append.call_args.args == ("tid",)
    assert append.call_args.kwargs["role"] == "assistant"
    assert append.call_args.kwargs["content"] == "a"


def test_a_dialog_without_a_thread_writes_to_no_thread():
    """A brand-new dialog must not leak its messages into another thread."""
    history = [{"content": "q"}]
    with patch.object(orchestrator_step, "append_thread_message") as append:
        cursor = orchestrator_step.persist_step_messages(None, history, 0)
    append.assert_not_called()
    assert cursor == 1


def test_the_cursor_does_not_move_backwards_on_an_empty_history():
    """A step that produced nothing leaves the cursor where it was."""
    with patch.object(orchestrator_step, "append_thread_message"):
        assert orchestrator_step.persist_step_messages("tid", [], 4) == 4


def test_a_second_write_of_the_same_step_is_a_no_op():
    """The cursor is what makes the write idempotent after a rerun."""
    history = [{"content": "q"}, {"content": "a"}]
    with patch.object(orchestrator_step, "append_thread_message") as append:
        first = orchestrator_step.persist_step_messages("tid", history, 0)
        second = orchestrator_step.persist_step_messages("tid", history, first)
    assert first == second == 2
    assert append.call_count == 2


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
