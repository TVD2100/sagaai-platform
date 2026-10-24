# -*- coding: utf-8 -*-
"""tests/scenarios/test_documents_and_delegation_scenario.py - end-to-end paths.

A user attaches a document and asks an employee to hand it to an assistant
and to delegate part of the work to a subagent. The scenario walks the two
paths the platform promises for that request and nothing internal:

Given an employee with one callable assistant and one spawnable subagent,
    and a DOCX document on disk
When the employee passes the document to ``invoke_assistant`` in ONE call
    and builds the prompt it will run with
Then the assistant receives the whole document text, and the prompt names
    both the subagent slug and the assistant id so the model can act without
    probing - and the document never needed ``run_code``.

The model calls are faked: the scenario measures the plumbing (document
extraction, call shape, prompt content), not provider behaviour.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from storage.db import reset_devagent_engine, reset_engine  # noqa: E402


def _docx_bytes(paragraphs) -> bytes:
    """Build a minimal real .docx payload with the given paragraphs."""
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def platform(tmp_path, monkeypatch):
    """An isolated data dir so orchestrators and assistants do not leak."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))

    import core.paths as paths_mod
    for attr, value in (("DATA_DIR", str(tmp_path)),
                        ("DB_PATH", str(tmp_path / "sagaai.db")),
                        ("DEVAGENT_DB_PATH", str(tmp_path / "devagent.db")),
                        ("HISTORY_DIR", str(tmp_path / "history")),
                        ("SYSTEM_PROMPTS_DIR", str(tmp_path / "system_prompts"))):
        monkeypatch.setattr(paths_mod, attr, value, raising=False)

    reset_engine()
    reset_devagent_engine()
    yield tmp_path
    reset_engine()
    reset_devagent_engine()


@pytest.fixture
def employee(platform):
    """An employee that may spawn one subagent and call one assistant."""
    from core.assistants import create_assistant
    from core.orchestrators import (create_orchestrator, set_allowed_agents,
                                    set_allowed_assistants)

    slug = "scenario_lead"
    assert create_orchestrator(slug, "Scenario Lead", "Leads the work") is not None
    assert create_orchestrator("scenario_helper", "Scenario Helper",
                              "Does half the work") is not None
    assert set_allowed_agents(slug, ["scenario_helper"])

    aid = create_assistant("Scenario Analyst", "yandex", "yandexgpt", 0.2,
                           "You analyse documents.")
    assert aid
    assert set_allowed_assistants(slug, [aid])
    return {"slug": slug, "assistant_id": aid}


def test_document_reaches_the_assistant_in_one_call(employee, tmp_path):
    """A DOCX file becomes context text - no run_code, no manual splitting."""
    from core.orchestration.invoke import _read_context_files

    path = tmp_path / "program.docx"
    path.write_bytes(_docx_bytes(["Раздел 1. Введение", "Раздел 2. Практика"]))

    context, skipped = _read_context_files([str(path)])

    assert skipped == []
    assert "### File: program.docx" in context
    assert "Раздел 1. Введение" in context
    assert "Раздел 2. Практика" in context


def test_missing_document_is_reported_not_fatal(employee, tmp_path):
    """A wrong path degrades to a skipped entry instead of a failed call."""
    from core.orchestration.invoke import _read_context_files

    context, skipped = _read_context_files([str(tmp_path / "absent.docx")])

    assert context == ""
    assert skipped == [str(tmp_path / "absent.docx")]


def test_prompt_names_both_the_subagent_and_the_assistant(employee):
    """The running prompt carries both identifiers - no probing step needed."""
    from core.orchestrators import build_assistant_dicts

    strong, _weak = build_assistant_dicts(employee["slug"])
    prompt = strong["text"]

    assert "## Available agents (spawn_agent)" in prompt
    assert "`scenario_helper`" in prompt
    assert "Scenario Helper" in prompt
    assert "## Available assistants (invoke_assistant)" in prompt
    assert "`%s`" % employee["assistant_id"] in prompt
    assert "Scenario Analyst" in prompt


def test_subagent_task_text_carries_absolute_paths(employee, tmp_path):
    """The delegation call hands the subagent absolute paths only."""
    from core.orchestration.contracts import AgentBrief
    from core.orchestration.service import _build_child_task_text

    document = tmp_path / "program.docx"
    document.write_bytes(_docx_bytes(["Раздел 1. Введение"]))
    brief = AgentBrief(goal="Подготовь конспект", brief="по документу",
                       artifacts_in={"программа": str(document)})

    text = _build_child_task_text(brief, str(tmp_path), "thread-parent")

    assert "Рабочая папка: %s" % tmp_path in text
    assert "программа: %s" % document in text


def test_document_needs_no_code_execution(employee, tmp_path):
    """The extraction path is the documented one: core.document_text itself."""
    from core.document_text import extract_text

    payload = _docx_bytes(["Раздел 3. Итоги"])
    text, warnings = extract_text("program.docx", payload)

    assert "Раздел 3. Итоги" in text
    assert warnings == []


def test_unknown_format_reports_a_warning_instead_of_raising(employee, tmp_path):
    """A format without an extractor degrades to a warning, not a crash."""
    from core.document_text import extract_text

    text, warnings = extract_text("archive.bin", b"\x00\x01\x02")

    assert text == ""
    assert warnings


def test_employee_without_assistant_rights_advertises_no_assistant_block(platform):
    """The prompt never promises a call the runtime would refuse."""
    from core.orchestrators import build_assistant_dicts, create_orchestrator

    assert create_orchestrator("scenario_solo", "Scenario Solo", "alone") is not None
    strong, _weak = build_assistant_dicts("scenario_solo")

    assert "## Available agents (spawn_agent)" in strong["text"]
    assert "## Available assistants" not in strong["text"]
