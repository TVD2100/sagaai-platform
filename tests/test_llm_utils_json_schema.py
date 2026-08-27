# tests.test_llm_utils_json_schema - json_schema plumbing in call_llm_with_system

import pytest


SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _send_request_stub(user_message, assistant=None, file_context="", **kwargs):
    _send_request_stub.last_kwargs = dict(kwargs)
    _send_request_stub.last_kwargs["assistant"] = assistant
    return "raw-response"


def test_direct_send_request_receives_json_schema_in_assistant():
    from dev_agent.llm_utils import call_llm_with_system

    result = call_llm_with_system(
        _send_request_stub, "hi", system="sys",
        json_schema=SCHEMA, json_schema_name="answer_v1",
    )
    assert result == "raw-response"
    assistant = _send_request_stub.last_kwargs["assistant"]
    assert assistant["json_schema"] == {"name": "answer_v1", "schema": SCHEMA}


def test_direct_send_request_passes_bare_schema_without_name():
    from dev_agent.llm_utils import call_llm_with_system

    call_llm_with_system(
        _send_request_stub, "hi", system="sys",
        json_schema=SCHEMA,
    )
    assistant = _send_request_stub.last_kwargs["assistant"]
    assert assistant["json_schema"] == SCHEMA


def test_no_json_schema_field_when_not_requested():
    from dev_agent.llm_utils import call_llm_with_system

    call_llm_with_system(_send_request_stub, "hi", system="sys")
    assistant = _send_request_stub.last_kwargs["assistant"]
    assert "json_schema" not in assistant


def test_adapter_form_call_unchanged():
    from dev_agent.llm_utils import call_llm_with_system

    captured = {}

    def adapter(user_message, system="", history=None):
        captured["kwargs"] = {"system": system, "history": history}
        return "adapter-ok"

    result = call_llm_with_system(
        adapter, "hi", system="sys", history=[{"role": "user", "content": "x"}],
        json_schema=SCHEMA,
    )
    assert result == "adapter-ok"
    assert captured["kwargs"] == {
        "system": "sys",
        "history": [{"role": "user", "content": "x"}],
    }
