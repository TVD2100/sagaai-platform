"""
tests.test_prompt_improver - tests for core.prompt_improver.

Covers the "Improve prompt" flow used by the Assistants page:
  - the built-in instruction file exists and is long enough;
  - improve_prompt_with_weak_model calls the weak DevAgent model with the
    instruction as system prompt and returns its output;
  - validation errors are raised for empty prompt / missing instruction /
    missing weak model / empty model output.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# --- helper to load the bundled instruction ---------------------------------

def _load_instruction_body():
    from core import defaults
    from core.orchestrators import DEVAGENT_SLUG
    path = os.path.join(
        defaults.orchestrators_dir(), DEVAGENT_SLUG, "instructions", "prompt_improver.md"
    )
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    _meta, body = defaults.parse_front_matter(raw, default_id="prompt_improver")
    return body


# --- basic module/instruction sanity ----------------------------------------

def test_instruction_file_is_long_enough():
    body = _load_instruction_body()
    assert len(body) > 500
    assert "## Role" in body
    assert "## Task" in body
    assert "Output format" in body


def test_get_improver_instruction_returns_text(monkeypatch):
    """When the instruction is stored in the orchestrator, get_improver_instruction returns it."""
    from core import prompt_improver

    captured = {}

    def fake_orch_get_instruction(slug, iid):
        captured["slug"] = slug
        captured["iid"] = iid
        return {"id": iid, "name": "Prompt Improver", "text": "## Role\nImprove prompts."}

    import core.orchestrators as orch_mod
    monkeypatch.setattr(orch_mod, "orch_get_instruction", fake_orch_get_instruction)

    text = prompt_improver.get_improver_instruction()
    assert text == "## Role\nImprove prompts."
    assert captured["slug"] == "dev_agent"
    assert captured["iid"] == "prompt_improver"


def test_get_improver_instruction_missing_returns_empty(monkeypatch):
    from core import prompt_improver
    import core.orchestrators as orch_mod
    monkeypatch.setattr(orch_mod, "orch_get_instruction", lambda slug, iid: None)
    assert prompt_improver.get_improver_instruction() == ""


# --- improve_prompt_with_weak_model -----------------------------------------

def test_improve_uses_weak_model_and_returns_text(monkeypatch):
    """The weak DevAgent assistant is used; its text is the instruction; the
    model output is returned trimmed."""
    from core import prompt_improver

    captured = {}
    instruction = "## Role\nYou improve prompts."

    def fake_send_request(user_message, assistant, file_context, history, lang):
        captured["user_message"] = user_message
        captured["service"] = assistant.get("service")
        captured["model"] = assistant.get("model")
        captured["sys_text"] = assistant.get("text")
        captured["history"] = history
        return "## Role\nImproved prompt.\n\n"

    def fake_build_assistant_dicts(slug):
        return (
            {"service": "StrongSvc", "model": "strong-model", "text": "strong"},
            {"service": "WeakSvc", "model": "weak-model", "text": "weak", "temperature": 0.4},
        )

    import core.orchestrators as orch_mod
    monkeypatch.setattr(orch_mod, "build_assistant_dicts", fake_build_assistant_dicts)

    result = prompt_improver.improve_prompt_with_weak_model(
        "## Role\nOld prompt.",
        lang="Russian",
        send_request_fn=fake_send_request,
        instruction_text=instruction,
    )

    assert result == "## Role\nImproved prompt."
    assert captured["service"] == "WeakSvc"
    assert captured["model"] == "weak-model"
    assert captured["sys_text"] == instruction
    assert captured["history"] == []
    assert "Old prompt." in captured["user_message"]
    assert "Improve the following assistant system prompt" in captured["user_message"]


def test_improve_empty_prompt_raises():
    from core import prompt_improver
    with pytest.raises(ValueError):
        prompt_improver.improve_prompt_with_weak_model(
            "   ", instruction_text="## Role\nImprove."
        )


def test_improve_missing_instruction_raises(monkeypatch):
    from core import prompt_improver
    monkeypatch.setattr(prompt_improver, "get_improver_instruction", lambda: "")
    with pytest.raises(ValueError):
        prompt_improver.improve_prompt_with_weak_model("## Role\nOld")


def test_improve_no_weak_model_raises(monkeypatch):
    from core import prompt_improver
    import core.orchestrators as orch_mod

    def fake_build_assistant_dicts(slug):
        return (
            {"service": "StrongSvc", "model": "strong-model", "text": "strong"},
            {"service": "", "model": "", "text": ""},
        )

    monkeypatch.setattr(orch_mod, "build_assistant_dicts", fake_build_assistant_dicts)
    with pytest.raises(ValueError):
        prompt_improver.improve_prompt_with_weak_model(
            "## Role\nOld", instruction_text="## Role\nImprove."
        )


def test_improve_empty_model_output_raises(monkeypatch):
    from core import prompt_improver

    def fake_send_request(user_message, assistant, file_context, history, lang):
        return "   "

    # No need to build real assistant dicts: pass a fake send_request and
    # an explicit instruction. The weak model check still needs a stub.
    import core.orchestrators as orch_mod

    def fake_build_assistant_dicts(slug):
        return (
            {"service": "S", "model": "M", "text": "s"},
            {"service": "W", "model": "WM", "text": "w"},
        )

    monkeypatch.setattr(orch_mod, "build_assistant_dicts", fake_build_assistant_dicts)
    with pytest.raises(ValueError):
        prompt_improver.improve_prompt_with_weak_model(
            "## Role\nOld",
            send_request_fn=fake_send_request,
            instruction_text="## Role\nImprove.",
        )
