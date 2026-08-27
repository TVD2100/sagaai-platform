# -*- coding: utf-8 -*-
# Scenario tests for structured output in DevAgent LLM consumers.
#
# Walks the public ToolExecutor.create_assistant_for_task flow like a user
# would: classification + Assistant Creator + service resolution + save.
# The LLM stubs enforce (or degrade from) the structured-output contract:
#   1. happy path  - provider honours json_schema; both LLM calls carry the
#                    correct schema and parsing uses the raw JSON contract.
#   2. edge case   - provider ignores the schema and wraps the answer in a
#                    fenced code block; the legacy fallback parser still
#                    saves the assistant.
#   3. error state - provider returns pure prose for the creator call; the
#                    tool reports a parse error and never saves an assistant.

import pytest

from dev_agent import config
from dev_agent.tool_executor import ToolExecutor


CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "complexity": {"type": "string", "enum": ["strong", "weak"]},
        "needs_web_search": {"type": "boolean"},
    },
    "required": ["complexity", "needs_web_search"],
    "additionalProperties": False,
}

ASSISTANT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "prompt": {"type": "string"},
    },
    "required": ["name", "description", "prompt"],
    "additionalProperties": False,
}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect DevAgent state into a temp sandbox."""
    root = tmp_path / "proj"
    monkeypatch.setattr(config, "PROJECT_ROOT", root)
    monkeypatch.setattr(config, "BACKUPS_DIR", root / "dev_agent" / "backups")
    monkeypatch.setattr(config, "WORKSPACE_DIR", root / "dev_agent" / "workspace")
    monkeypatch.setattr(config, "CHANGELOG_FILE", root / "CHANGELOG.md")
    monkeypatch.setattr(config, "PROTECTED_FILES", ())
    config.ensure_runtime_dirs()
    return root


def _wire_flow(monkeypatch, fake_llm, fake_create):
    """Patch the LLM plumbing and DB writes for a scenario run."""
    import dev_agent.assistant_model_resolver as resolver
    import dev_agent.tool_executor as te_module

    monkeypatch.setattr(resolver, "call_llm_with_system", fake_llm)
    monkeypatch.setattr(te_module, "call_llm_with_system", fake_llm)
    monkeypatch.setattr(te_module, "get_instruction_prompt", lambda iid: "creator")
    monkeypatch.setattr(
        te_module,
        "resolve_service_model_for_assistant",
        lambda task, complexity, needs_web_search: (
            "Svc", "model-x", [], "Using Svc > model-x.", False, None, None,
        ),
    )
    saved = {}

    def _record_and_create(**kwargs):
        saved["kwargs"] = kwargs
        return fake_create(**kwargs)

    monkeypatch.setattr(te_module, "create_assistant", _record_and_create)
    monkeypatch.setattr(
        te_module,
        "get_assistant_by_id",
        lambda aid: {
            "id": aid,
            "name": saved.get("kwargs", {}).get("name", "Test Assistant"),
            "description": saved.get("kwargs", {}).get("description", ""),
            "text": saved.get("kwargs", {}).get("text", "p" * 60),
        },
    )
    te = ToolExecutor()
    te.set_send_request(lambda *a, **k: None)
    return te


# --- Scenario 1: happy path ---------------------------------------------------


def test_structured_output_full_creation_flow(sandbox, monkeypatch):
    """Given a provider that honours json_schema, when a user creates an
    assistant, then both LLM calls carry the native schema and the assistant
    is saved."""
    calls = []
    create_calls = {}

    def fake_llm(send_request_fn, user_message, system="", history=None, **kwargs):
        calls.append(dict(kwargs))
        if kwargs.get("json_schema_name") == "classification_result":
            return "{\"complexity\": \"weak\", \"needs_web_search\": false}"
        prompt = "p" * 60
        return (
            "{\"name\": \"Test Assistant\", \"description\": \"does things\", "
            + "\"prompt\": \"" + prompt + "\"}"
        )

    def fake_create(**kwargs):
        create_calls["kwargs"] = kwargs
        return "new-id-123"

    te = _wire_flow(monkeypatch, fake_llm, fake_create)
    result = te.create_assistant_for_task("Build a simple translator")

    assert result["ok"] is True
    assert result["assistant_name"] == "Test Assistant"
    assert {c["json_schema_name"] for c in calls} == {
        "classification_result",
        "assistant_creation",
    }
    by_name = {c["json_schema_name"]: c["json_schema"] for c in calls}
    assert by_name["classification_result"] == CLASSIFICATION_SCHEMA
    assert by_name["assistant_creation"] == ASSISTANT_SCHEMA
    assert create_calls["kwargs"]["name"] == "Test Assistant"
    assert create_calls["kwargs"]["service"] == "Svc"
    assert create_calls["kwargs"]["model"] == "model-x"


# --- Scenario 2: schema ignored, fenced fallback -----------------------------


def test_fenced_json_fallback_still_creates_assistant(sandbox, monkeypatch):
    """Given a provider that wraps the answer in a markdown fence, when the
    same creation flow runs, then the fallback parser recovers the payload
    and the assistant is saved."""
    create_calls = {}

    def fake_llm(send_request_fn, user_message, system="", history=None, **kwargs):
        if kwargs.get("json_schema_name") == "classification_result":
            return "```json\n{\"complexity\": \"strong\", \"needs_web_search\": false}\n```"
        prompt = "q" * 60
        return (
            "```json\n{\"name\": \"Fenced Assistant\", "
            + "\"description\": \"wrapped in fence\", "
            + "\"prompt\": \"" + prompt + "\"}\n```"
        )

    def fake_create(**kwargs):
        create_calls["kwargs"] = kwargs
        return "new-id-456"

    te = _wire_flow(monkeypatch, fake_llm, fake_create)
    result = te.create_assistant_for_task("Write documentation")

    assert result["ok"] is True
    assert result["assistant_name"] == "Fenced Assistant"
    assert create_calls["kwargs"]["name"] == "Fenced Assistant"


# --- Scenario 3: creator returns prose -> graceful error ----------------------


def test_prose_creator_response_reports_parse_error(sandbox, monkeypatch):
    """Given a creator LLM that returns pure prose, when the creation flow
    runs, then the tool reports a parse error and never saves an assistant."""
    created = []

    def fake_llm(send_request_fn, user_message, system="", history=None, **kwargs):
        if kwargs.get("json_schema_name") == "classification_result":
            return "{\"complexity\": \"weak\", \"needs_web_search\": false}"
        return "Sorry, I cannot generate that right now."

    def fake_create(**kwargs):
        created.append(kwargs)
        return "new-id-789"

    te = _wire_flow(monkeypatch, fake_llm, fake_create)
    result = te.create_assistant_for_task("Do something")

    assert result["ok"] is False
    assert "parse" in result["error"].lower()
    assert created == []
