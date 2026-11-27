"""
Scenario tests: the wire shape a GigaChat request vs a plain provider request.

A real dialog sends the orchestrator prompt first and the hidden platform
snapshots (the task journal, the thread artifacts folder) after it. GigaChat
keeps only the last ``system`` message, so on those models the snapshots
replace the prompt and the assistant loses its role, its tools and its rules.
The catalog declares those models; every other provider must receive the
conversation exactly as it was assembled.
"""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import pytest

from core.api_chat import SNAPSHOT_MARKER
from core.api_layer import _request_bearer
from core.assistant_tools import run_bearer_tool_loop

PROMPT = "# DevAgent - System Prompt\n\nYou are DevAgent."
TASK_SNAPSHOT = "CURRENT TASK STATE:\nthread_id: abc\ntask_state_file: /tmp/x.md"
ARTIFACTS_SNAPSHOT = "## CURRENT THREAD ARTIFACTS DIR\n\nthread_id: abc\nthread_files_dir: /tmp/files"


def _history():
    return [
        {"role": "system", "content": TASK_SNAPSHOT, "hidden": True},
        {"role": "system", "content": ARTIFACTS_SNAPSHOT, "hidden": True},
        {"role": "user", "content": "Кто ты?", "_index": 2},
        {"role": "assistant", "content": "Я DevAgent.", "_index": 3},
    ]


def _require(req, model, service_name, svc_extra=None):
    svc = {"name": service_name, "auth_type": "bearer",
           "config_key": "FAKE_API_KEY", "env_key_fields": ["config_key"],
           "base_url": "https://example.invalid/v1/chat/completions"}
    if svc_extra is not None:
        svc.update(svc_extra)
    req.update({
        "svc": svc,
        "cfg": {"FAKE_API_KEY": "k"},
        "svc_name": service_name,
        "model": model,
        "base_url": svc["base_url"],
        "sys_text": PROMPT,
        "hist_msgs": _history(),
        "user_content": "Кто ты?",
        "temp": 0.2,
        "max_tokens": 100,
        "native_function_tools": False,
        "tools_list": None,
        "max_tool_calls": None,
        "usage_callback": None,
        "tool_choice": None,
        "json_schema": None,
        "assistant": {},
        "on_tool_call": None,
        "retry_callback": None,
    })
    return req


class _Captured:
    """Stand-in for requests.post that records the payload and answers 200."""

    def __init__(self):
        self.payloads = []

    def __call__(self, url, **kwargs):
        self.payloads.append(kwargs.get("json"))

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"role": "assistant",
                                                    "content": "ok"}}]}

        return _Response()


@pytest.fixture()
# No cleanup is needed: the stand-in lives only inside the test process.
def captured_post(monkeypatch):
    capture = _Captured()
    import core.api_bearer as bearer_mod
    monkeypatch.setattr(bearer_mod.requests, "post", capture)
    return capture


def _system_count(payload):
    return sum(1 for m in payload["messages"] if m.get("role") == "system")


# ─── GigaChat models are collapsed ──────────────────────────────────────────

@pytest.mark.parametrize("model", [
    "GigaChat/GigaChat-4-708B-A41",
    "ai-sage/GigaChat3.5-432B-A28B",
])
def test_cloud_gigachat_sends_exactly_one_system(captured_post, model):
    rule = {"single_system_message": {"models": ["GigaChat/", "ai-sage/GigaChat"]}}
    req = _require({}, model, "Cloud", rule)
    _request_bearer(req)
    payload = captured_post.payloads[0]
    assert _system_count(payload) == 1
    assert payload["messages"][0]["content"].startswith(PROMPT)


def test_gigachat_ai_studio_sends_exactly_one_system(captured_post):
    rule = {"single_system_message": {"models": ["GigaChat"]}}
    req = _require({}, "GigaChat-4-708B-A41B", "gigachat_ai_studio", rule)
    _request_bearer(req)
    payload = captured_post.payloads[0]
    assert _system_count(payload) == 1
    assert payload["messages"][0]["content"].startswith(PROMPT)


def test_both_snapshots_survive_as_platform_context(captured_post):
    rule = {"single_system_message": {"models": ["GigaChat/"]}}
    req = _require({}, "GigaChat/GigaChat-4-708B-A41", "Cloud", rule)
    _request_bearer(req)
    messages = captured_post.payloads[0]["messages"]
    # The prompt is the ONLY system message and stays byte-identical from
    # step to step: a prefix cache is reusable only while the opening bytes
    # do not change.
    assert messages[0] == {"role": "system", "content": PROMPT}
    # Snapshots travel in their own marked messages, in arrival order.
    snapshots = [m for m in messages[1:] if m.get("role") == "user"
                 and SNAPSHOT_MARKER in (m.get("content") or "")]
    assert len(snapshots) == 2
    assert "CURRENT TASK STATE" in snapshots[0]["content"]
    assert "CURRENT THREAD ARTIFACTS DIR" in snapshots[1]["content"]
    # The user's own turn is passed through untouched.
    assert messages[-1] == {"role": "user", "content": "Кто ты?"}


# ─── Every other model keeps the assembled conversation ─────────────────────

@pytest.mark.parametrize("model", [
    "openai/gpt-5.4",
    "anthropic/claude-opus-4.8",
    "deepseek-ai/DeepSeek-V4-Pro",
    "google/gemini-2.5-pro",
    "meta-llama/llama-3.3-70b-instruct",
])
def test_cloud_non_gigachat_keeps_every_system(captured_post, model):
    rule = {"single_system_message": {"models": ["GigaChat/", "ai-sage/GigaChat"]}}
    req = _require({}, model, "Cloud", rule)
    _request_bearer(req)
    payload = captured_post.payloads[0]
    assert _system_count(payload) == 3
    assert payload["messages"][0]["content"] == PROMPT
    assert payload["messages"][1]["content"] == TASK_SNAPSHOT
    assert payload["messages"][2]["content"] == ARTIFACTS_SNAPSHOT


def test_service_without_the_rule_keeps_every_system(captured_post):
    req = _require({}, "GigaChat-4-708B-A41B", "some_bearer", None)
    _request_bearer(req)
    assert _system_count(captured_post.payloads[0]) == 3


# ─── The tool loop applies the same rule ────────────────────────────────────

def _record_post(records):
    """Build a stand-in for _post_chat_completions that records the payload."""
    def _post(url, api_key, payload, svc_name, retry_callback, extra_headers=None):
        records.append(payload)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    return _post


def test_tool_loop_collapses_for_gigachat(monkeypatch):
    records = []
    import core.assistant_tools as tools_mod
    monkeypatch.setattr(tools_mod, "_post_chat_completions", _record_post(records))
    run_bearer_tool_loop(
        "https://example.invalid/v1/chat/completions", "k", "GigaChat/GigaChat-4-708B-A41",
        PROMPT, _history(), "Кто ты?", temperature=0.2, max_tokens=100,
        single_system=True,
    )
    assert _system_count(records[0]) == 1
    assert records[0]["messages"][0]["content"].startswith(PROMPT)
    assert records[0]["messages"][-1] == {"role": "user", "content": "Кто ты?"}


def test_tool_loop_keeps_every_system_by_default(monkeypatch):
    records = []
    import core.assistant_tools as tools_mod
    monkeypatch.setattr(tools_mod, "_post_chat_completions", _record_post(records))
    run_bearer_tool_loop(
        "https://example.invalid/v1/chat/completions", "k", "openai/gpt-5.4",
        PROMPT, _history(), "Кто ты?", temperature=0.2, max_tokens=100,
    )
    assert _system_count(records[0]) == 3
