# -*- coding: utf-8 -*-
"""tests/test_orchestration_invoke.py - the one-shot assistant call.

The boundary under test is narrow and easy to erode: ``invoke_once`` must
make exactly ONE model call, must not apply the assistant's tools or
knowledge bases, and must never truncate the caller's context silently.
The transport is replaced by a fake ``send_request_fn``, so the assertions
read the payload the provider WOULD receive.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import invoke as I  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    ERR_CONTEXT_TOO_LARGE,
    ERR_INVALID_ARGS,
    ERR_UNKNOWN_ASSISTANT,
    InvokeRequest,
)


class FakeTransport:
    """Record every provider call and answer with a fixed text."""

    def __init__(self, text="answer", raises=None):
        self.calls = []
        self.text = text
        self.raises = raises

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        callback = kwargs.get("usage_callback")
        if callable(callback):
            callback({"in": 12, "out": 7})
        return self.text


@pytest.fixture
def cards(monkeypatch):
    """Control the assistant card and the parent orchestrator model."""
    state = {"cards": {}, "parent": {"service": "yandex", "model": "parent-model"}}

    import core.assistants as assistants_mod
    import core.orchestrators as orch_mod

    monkeypatch.setattr(assistants_mod, "get_assistant_by_id",
                        lambda aid: state["cards"].get(aid))
    monkeypatch.setattr(orch_mod, "build_assistant_dicts",
                        lambda slug="dev_agent": (dict(state["parent"]), {}))
    return state


# ─── the assistant card is used, the database is not touched ────────────────


def test_card_supplies_prompt_model_and_temperature(cards):
    """Test 6: prompt and model come from the card; nothing is written."""
    cards["cards"]["abcd1234"] = {
        "id": "abcd1234", "name": "Helper", "service": "openai",
        "model": "gpt-x", "temperature": 0.4, "text": "You are Helper.",
        "tools": [], "max_tokens": 300,
    }
    transport = FakeTransport("the answer")

    result = I.invoke_once(InvokeRequest(assistant_id="abcd1234", user="hi"),
                           parent_slug="dev_agent", send_request_fn=transport)

    assert result["ok"] is True
    assert result["result"]["text"] == "the answer"
    assert result["result"]["assistant"] == {"id": "abcd1234", "name": "Helper"}

    sent = transport.calls[0]
    assert sent["assistant"]["text"] == "You are Helper."
    assert sent["assistant"]["service"] == "openai"
    assert sent["assistant"]["model"] == "gpt-x"
    assert sent["assistant"]["temperature"] == 0.4
    assert sent["assistant"]["max_tokens"] == 300
    assert sent["user_message"] == "hi"
    assert sent["history"] == []
    assert sent["file_context"] == ""


def test_card_does_not_carry_tools_or_identity_into_the_request(cards):
    """Test 9: an assistant's tools are NOT applied by a one-shot call."""
    cards["cards"]["tooled01"] = {
        "id": "tooled01", "name": "Tooled", "service": "openai",
        "model": "gpt-x", "temperature": 0.2, "text": "prompt",
        "tools": ["web_search", "generate_image"],
        "max_tool_calls": 5, "json_schema": {"type": "object"},
    }
    transport = FakeTransport()

    I.invoke_once(InvokeRequest(assistant_id="tooled01", user="hi"),
                  send_request_fn=transport)

    sent_card = transport.calls[0]["assistant"]
    for forbidden in ("id", "slug", "tools", "max_tool_calls", "json_schema",
                      "tool_choice"):
        assert forbidden not in sent_card
    assert set(sent_card) <= set(I._ALLOWED_CARD_KEYS)


def test_no_rag_block_reaches_the_prompt(cards):
    """Test 11: a RAG-bound assistant gets no knowledge-base block here."""
    cards["cards"]["rag00001"] = {
        "id": "rag00001", "name": "Raggy", "service": "openai",
        "model": "gpt-x", "temperature": 0.2, "text": "prompt",
        "rag_base": "marketing", "slug": "raggy",
    }
    transport = FakeTransport()

    I.invoke_once(InvokeRequest(assistant_id="rag00001", user="hi"),
                  send_request_fn=transport)

    sent = transport.calls[0]
    assert "База знаний" not in sent["user_message"]
    assert "slug" not in sent["assistant"]
    assert "id" not in sent["assistant"]
    assert len(transport.calls) == 1  # no tool loop, no second round-trip


def test_unknown_assistant_is_reported(cards):
    result = I.invoke_once(InvokeRequest(assistant_id="missing1", user="hi"),
                           send_request_fn=FakeTransport())
    assert result["error_code"] == ERR_UNKNOWN_ASSISTANT
    assert result["assistant"] == "missing1"


# ─── service/model resolution order ────────────────────────────────────────


def test_explicit_model_overrides_the_card(cards):
    cards["cards"]["abcd1234"] = {"id": "abcd1234", "name": "H",
                                 "service": "openai", "model": "card-model",
                                 "text": "p"}
    transport = FakeTransport()

    I.invoke_once(InvokeRequest(assistant_id="abcd1234", user="hi",
                                model="call-model"),
                  send_request_fn=transport)

    assert transport.calls[0]["assistant"]["model"] == "call-model"
    assert transport.calls[0]["assistant"]["service"] == "openai"


def test_ephemeral_call_falls_back_to_the_parent_model(cards):
    transport = FakeTransport()

    I.invoke_once(InvokeRequest(system="be terse", user="hi"),
                  parent_slug="dev_agent", send_request_fn=transport)

    sent = transport.calls[0]["assistant"]
    assert sent["service"] == "yandex"
    assert sent["model"] == "parent-model"
    assert sent["text"] == "be terse"


def test_unresolvable_service_or_model_is_invalid_args(cards):
    """Test 20: no service/model anywhere is a caller error, not a crash."""
    cards["parent"] = {"service": "", "model": ""}
    transport = FakeTransport()

    result = I.invoke_once(InvokeRequest(system="be terse", user="hi"),
                           parent_slug="dev_agent", send_request_fn=transport)

    assert result["error_code"] == ERR_INVALID_ARGS
    assert result["missing"] == ["service", "model"]
    assert transport.calls == []


def test_missing_service_only_is_reported(cards):
    cards["parent"] = {"service": "", "model": "parent-model"}
    result = I.invoke_once(InvokeRequest(system="p", user="hi"),
                           send_request_fn=FakeTransport())
    assert result["missing"] == ["service"]


def test_assistant_id_and_system_are_mutually_exclusive(cards):
    result = I.invoke_once(InvokeRequest(assistant_id="abcd1234", system="s",
                                         user="hi"),
                           send_request_fn=FakeTransport())
    assert result["error_code"] == ERR_INVALID_ARGS
    assert result["conflicting"] == ["assistant_id", "system"]


# ─── context: complete, never truncated ────────────────────────────────────


def test_large_context_is_passed_through_whole(cards, tmp_path):
    """Test 12: 100 000 characters arrive intact, without a truncation mark."""
    big = "A" * 100000
    path = tmp_path / "big.txt"
    path.write_text(big, encoding="utf-8")
    transport = FakeTransport()

    result = I.invoke_once(
        InvokeRequest(system="analyse", user="go", context_files=[str(path)]),
        send_request_fn=transport)

    assert result["ok"] is True
    sent = transport.calls[0]["user_message"]
    assert big in sent
    assert "...[truncated]..." not in sent
    assert len(sent) > 100000


def test_window_overflow_is_refused_as_context_too_large(cards, tmp_path):
    """Test 12b: an oversized payload is refused, never cut."""
    from core.api_errors import ContextWindowError

    path = tmp_path / "big.txt"
    path.write_text("B" * 5000, encoding="utf-8")
    transport = FakeTransport(raises=ContextWindowError(window=8000, tokens=9000))

    result = I.invoke_once(
        InvokeRequest(system="analyse", user="go", context_files=[str(path)]),
        send_request_fn=transport)

    assert result["ok"] is False
    assert result["error_code"] == ERR_CONTEXT_TOO_LARGE
    assert result["window"] == 8000
    assert result["tokens"] == 9000


def test_context_files_use_the_file_block_format(cards, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("line one\nline two", encoding="utf-8")
    transport = FakeTransport()

    I.invoke_once(InvokeRequest(system="p", user="go",
                                context_files=[str(path)]),
                  send_request_fn=transport)

    message = transport.calls[0]["user_message"]
    assert "### File: notes" in message
    assert "line one" in message
    assert "**Контекст из файлов:**" in message


def test_missing_context_file_is_reported_not_fatal(cards, tmp_path):
    transport = FakeTransport()

    result = I.invoke_once(
        InvokeRequest(system="p", user="go",
                      context_files=[str(tmp_path / "absent.txt")]),
        send_request_fn=transport)

    assert result["ok"] is True
    assert result["result"]["skipped_files"]
    assert transport.calls[0]["user_message"].strip() == "go"


def test_context_only_call_needs_no_user_text(cards, tmp_path):
    path = tmp_path / "data.txt"
    path.write_text("payload", encoding="utf-8")
    transport = FakeTransport()

    result = I.invoke_once(
        InvokeRequest(system="p", context_files=[str(path)]),
        send_request_fn=transport)

    assert result["ok"] is True
    assert "payload" in transport.calls[0]["user_message"]


# ─── provider failures ─────────────────────────────────────────────────────


def test_provider_error_is_mapped_without_a_traceback(cards):
    transport = FakeTransport(raises=RuntimeError("boom"))

    result = I.invoke_once(InvokeRequest(system="p", user="hi"),
                           send_request_fn=transport)

    assert result["ok"] is False
    assert result["error_code"] == "provider_error"
    assert "RuntimeError" in result["error"]
    assert "Traceback" not in result["error"]


def test_usage_callback_is_collected_into_tokens(cards):
    transport = FakeTransport()

    result = I.invoke_once(InvokeRequest(system="p", user="hi"),
                           send_request_fn=transport)

    assert result["result"]["tokens"] == {"in": 12, "out": 7}


# ─── documents travel as files, not as pasted text ─────────────────────────


def _docx_bytes(paragraphs, tables=None) -> bytes:
    """Build a small DOCX payload; skip the test when python-docx is absent."""
    import io

    docx = pytest.importorskip("docx")
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    for rows in tables or []:
        table = document.add_table(rows=len(rows), cols=len(rows[0]))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                table.cell(r, c).text = str(value)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_context_file_is_extracted_in_one_call(cards, tmp_path):
    """One DOCX in context_files reaches the assistant as text - one call."""
    path = tmp_path / "program.docx"
    path.write_bytes(_docx_bytes(
        ["Балльно-рейтинговая система"],
        tables=[[['Раздел', 'Баллы'], ['Семинары', '40']]],
    ))
    transport = FakeTransport()

    result = I.invoke_once(
        InvokeRequest(system="p", user="оцени программу",
                      context_files=[str(path)]),
        send_request_fn=transport)

    assert result["ok"] is True
    assert len(transport.calls) == 1
    message = transport.calls[0]["user_message"]
    assert "### File: program.docx" in message
    assert "Балльно-рейтинговая система" in message
    assert "Семинары,40" in message
    assert "skipped_files" not in result["result"]


def test_xlsx_context_file_keeps_every_sheet(cards, tmp_path):
    """A workbook is rendered as CSV per sheet inside the same call."""
    import io

    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Оценки"
    sheet.append(["Студент", "Балл"])
    sheet.append(["Иванов", 40])
    buffer = io.BytesIO()
    workbook.save(buffer)
    path = tmp_path / "grades.xlsx"
    path.write_bytes(buffer.getvalue())
    transport = FakeTransport()

    I.invoke_once(InvokeRequest(system="p", user="go",
                                context_files=[str(path)]),
                  send_request_fn=transport)

    message = transport.calls[0]["user_message"]
    assert "### Sheet: Оценки" in message
    assert "Иванов,40" in message


def test_plain_text_file_still_uses_the_text_reader(cards, tmp_path):
    """A .txt file keeps the original utf-8 path and file-block format."""
    path = tmp_path / "notes.txt"
    path.write_text("строка", encoding="utf-8")
    transport = FakeTransport()

    I.invoke_once(InvokeRequest(system="p", user="go",
                                context_files=[str(path)]),
                  send_request_fn=transport)

    message = transport.calls[0]["user_message"]
    assert "### File: notes" in message
    assert "строка" in message


def test_unreadable_document_is_reported_as_skipped(cards, tmp_path):
    """A corrupt document does not abort the call: it lands in skipped_files."""
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a zip at all")
    transport = FakeTransport()

    result = I.invoke_once(
        InvokeRequest(system="p", user="go", context_files=[str(path)]),
        send_request_fn=transport)

    assert result["ok"] is True
    assert str(path) in result["result"]["skipped_files"]
    assert transport.calls[0]["user_message"].strip() == "go"
