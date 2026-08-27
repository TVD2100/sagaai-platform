# tests.test_structured_output_consumers - structured output adoption by
# DevAgent LLM consumers (assistant classifier + Assistant Creator).
#
# Verifies that both consumers pass a native JSON Schema to
# call_llm_with_system and that the legacy fallback parsing still works.

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


# --- resolver: classify_assistant_requirements --------------------------------


def test_classify_passes_json_schema_and_name(monkeypatch):
    import dev_agent.assistant_model_resolver as resolver

    captured = {}

    def fake_call_llm(send_request_fn, user_message, system="", history=None, **kwargs):
        captured["kwargs"] = kwargs
        return '{"complexity": "weak", "needs_web_search": true}'

    monkeypatch.setattr(resolver, "call_llm_with_system", fake_call_llm)

    result = resolver.classify_assistant_requirements(
        "Translate user messages", send_request_fn=lambda *a, **k: None,
    )

    assert result == {"complexity": "weak", "needs_web_search": True}
    assert captured["kwargs"]["json_schema"] == resolver.CLASSIFICATION_SCHEMA
    assert captured["kwargs"]["json_schema_name"] == "classification_result"


def test_classify_fenced_json_still_parsed(monkeypatch):
    import dev_agent.assistant_model_resolver as resolver

    def fake_call_llm(*a, **k):
        return '```json\n{"complexity": "strong", "needs_web_search": false}\n```'

    monkeypatch.setattr(resolver, "call_llm_with_system", fake_call_llm)
    result = resolver.classify_assistant_requirements("Write code", lambda *a, **k: None)
    assert result == {"complexity": "strong", "needs_web_search": False}


def test_classify_exception_falls_back_to_defaults(monkeypatch):
    import dev_agent.assistant_model_resolver as resolver

    def boom(*a, **k):
        raise RuntimeError("no backend")

    monkeypatch.setattr(resolver, "call_llm_with_system", boom)
    result = resolver.classify_assistant_requirements("Anything", lambda *a, **k: None)
    assert result == {"complexity": "strong", "needs_web_search": False}


def test_classify_keyword_hints_when_json_invalid(monkeypatch):
    import dev_agent.assistant_model_resolver as resolver

    monkeypatch.setattr(
        resolver,
        "call_llm_with_system",
        lambda *a, **k: "Definitely not JSON, just prose.",
    )
    result = resolver.classify_assistant_requirements(
        "Simple translation helper", lambda *a, **k: None,
    )
    assert result == {"complexity": "weak", "needs_web_search": False}


# --- Assistant Creator: _create_assistant_with_auto_model ---------------------


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


def test_assistant_creator_passes_json_schema_and_creates(sandbox, monkeypatch):
    import dev_agent.tool_executor as te_module

    captured = {}
    create_calls = {}

    def fake_prompt(instruction_id):
        return "You are the Assistant Creator."

    def fake_call_llm(send_request_fn, user_message, system="", history=None, **kwargs):
        captured["kwargs"] = kwargs
        prompt = "p" * 60
        return (
            '{"name": "Test Assistant", "description": "does things", '
            f'"prompt": "{prompt}"' + "}"
        )

    def fake_create_assistant(**kwargs):
        create_calls["kwargs"] = kwargs
        return "new-id-123"

    monkeypatch.setattr(te_module, "get_instruction_prompt", fake_prompt)
    monkeypatch.setattr(te_module, "call_llm_with_system", fake_call_llm)
    monkeypatch.setattr(
        te_module,
        "classify_assistant_requirements",
        lambda task, fn: {"complexity": "weak", "needs_web_search": False},
    )
    monkeypatch.setattr(
        te_module,
        "resolve_service_model_for_assistant",
        lambda task, complexity, needs_web_search: (
            "Svc", "model-x", [], "Using Svc > model-x.", False, None, None,
        ),
    )
    monkeypatch.setattr(te_module, "create_assistant", fake_create_assistant)
    monkeypatch.setattr(
        te_module,
        "get_assistant_by_id",
        lambda aid: {
            "id": aid,
            "name": "Test Assistant",
            "description": "does things",
            "text": "p" * 60,
        },
    )

    te = ToolExecutor()
    te.set_send_request(lambda *a, **k: None)
    result = te._create_assistant_with_auto_model("Build a simple translator")

    assert result["ok"] is True
    assert result["assistant_name"] == "Test Assistant"
    assert captured["kwargs"]["json_schema"] == ASSISTANT_SCHEMA
    assert captured["kwargs"]["json_schema_name"] == "assistant_creation"
    assert create_calls["kwargs"]["name"] == "Test Assistant"
    assert create_calls["kwargs"]["service"] == "Svc"
    assert create_calls["kwargs"]["model"] == "model-x"


def test_assistant_creator_parse_failure_returns_error(sandbox, monkeypatch):
    import dev_agent.tool_executor as te_module

    monkeypatch.setattr(te_module, "get_instruction_prompt", lambda iid: "creator")
    monkeypatch.setattr(
        te_module,
        "call_llm_with_system",
        lambda *a, **k: "no json here, sorry",
    )

    te = ToolExecutor()
    te.set_send_request(lambda *a, **k: None)
    result = te._create_assistant_with_auto_model("Do something")

    assert result["ok"] is False
    assert "parse" in result["error"].lower()
