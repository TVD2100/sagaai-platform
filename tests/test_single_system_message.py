"""
Unit tests for the single-system-message normalisation (core.api_chat).

A provider may keep only the last ``system`` message of a request. The
platform sends the orchestrator prompt first and its hidden snapshots after
it, so on such a provider the short snapshot wins and the prompt is lost. The
normalisation merges every system text into the leading system message; the
catalog decides which services get it.
"""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import copy

from core.api_chat import (
    SNAPSHOT_MARKER,
    build_chat_payload,
    collapse_system_messages,
    needs_single_system,
)


def _roles(messages):
    return [m.get("role") for m in messages]


# ─── collapse_system_messages ───────────────────────────────────────────────

def test_one_system_message_is_unchanged():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "user", "content": "hello"},
    ]
    out = collapse_system_messages(messages)
    assert out == messages
    assert _roles(out) == ["system", "user"]


def test_two_system_messages_merge_into_the_leading_one():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "CURRENT TASK STATE: x"},
        {"role": "user", "content": "hello"},
    ]
    out = collapse_system_messages(messages)
    assert _roles(out) == ["system", "user"]
    head = out[0]["content"]
    assert head.startswith("PROMPT")
    assert SNAPSHOT_MARKER in head
    assert "CURRENT TASK STATE: x" in head


def test_three_system_messages_keep_their_order():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "SNAP-1"},
        {"role": "system", "content": "SNAP-2"},
        {"role": "user", "content": "hello"},
    ]
    out = collapse_system_messages(messages)
    assert _roles(out) == ["system", "user"]
    head = out[0]["content"]
    assert head.index("PROMPT") < head.index("SNAP-1") < head.index("SNAP-2")


def test_the_leading_prompt_is_kept_verbatim():
    prompt = "# DevAgent - System Prompt\n\nrole text"
    messages = [
        {"role": "system", "content": prompt},
        {"role": "system", "content": "snapshot"},
        {"role": "user", "content": "q"},
    ]
    out = collapse_system_messages(messages)
    assert out[0]["content"].startswith(prompt)


def test_the_user_turn_is_never_rewritten():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "snapshot"},
        {"role": "user", "content": "hello"},
    ]
    out = collapse_system_messages(messages)
    assert out[-1] == {"role": "user", "content": "hello"}


def test_a_late_system_message_is_absorbed_and_the_turns_stay_put():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "user", "content": "first"},
        {"role": "system", "content": "late snapshot"},
        {"role": "assistant", "content": "answer"},
    ]
    out = collapse_system_messages(messages)
    assert _roles(out) == ["system", "user", "assistant"]
    assert "late snapshot" in out[0]["content"]
    assert out[1]["content"] == "first"


def test_trailing_snapshot_joins_the_head():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "user", "content": "hello"},
        {"role": "system", "content": "trailing"},
    ]
    out = collapse_system_messages(messages)
    assert _roles(out) == ["system", "user"]
    assert "trailing" in out[0]["content"]


def test_no_user_message_at_all_still_yields_one_system():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "snapshot"},
    ]
    out = collapse_system_messages(messages)
    assert _roles(out) == ["system"]
    assert "snapshot" in out[0]["content"]


def test_empty_list_and_garbage_entries():
    assert collapse_system_messages([]) == []
    out = collapse_system_messages(["nonsense", {"role": "user", "content": "x"}])
    assert _roles(out) == ["user"]


def test_input_is_not_mutated():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "snapshot"},
        {"role": "user", "content": "hello"},
    ]
    before = copy.deepcopy(messages)
    collapse_system_messages(messages)
    assert messages == before
    assert _roles(messages) == ["system", "system", "user"]


def test_empty_snapshot_text_is_dropped():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "   "},
        {"role": "user", "content": "hello"},
    ]
    out = collapse_system_messages(messages)
    assert _roles(out) == ["system", "user"]
    assert out[0]["content"] == "PROMPT"
    assert out[1]["content"] == "hello"


# ─── needs_single_system ────────────────────────────────────────────────────

def test_no_declaration_means_no_collapse():
    assert needs_single_system({}, "any-model") is False
    assert needs_single_system(None, "any-model") is False
    assert needs_single_system({"name": "Cloud"}, "GigaChat/GigaChat-4") is False


def test_true_applies_to_every_model():
    svc = {"single_system_message": True}
    assert needs_single_system(svc, "anything") is True
    assert needs_single_system(svc, "") is True


def test_prefix_list_matches_only_its_models():
    svc = {"single_system_message": {"models": ["GigaChat/", "ai-sage/GigaChat"]}}
    assert needs_single_system(svc, "GigaChat/GigaChat-4-708B-A41") is True
    assert needs_single_system(svc, "ai-sage/GigaChat3.5-432B-A28B") is True
    assert needs_single_system(svc, "openai/gpt-5.4") is False
    assert needs_single_system(svc, "anthropic/claude-opus-4.8") is False
    assert needs_single_system(svc, "deepseek/DeepSeek-V3") is False


def test_single_string_prefix_is_accepted():
    svc = {"single_system_message": {"models": "GigaChat"}}
    assert needs_single_system(svc, "GigaChat-4-708B-A41B") is True
    assert needs_single_system(svc, "Qwen2.5-72B") is False


def test_malformed_rule_is_false():
    assert needs_single_system({"single_system_message": "yes"}, "m") is False
    assert needs_single_system({"single_system_message": False}, "m") is False
    assert needs_single_system({"single_system_message": {"models": []}}, "m") is False


# ─── build_chat_payload ─────────────────────────────────────────────────────

def test_payload_keeps_every_system_without_the_flag():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "snapshot"},
        {"role": "user", "content": "hello"},
    ]
    payload, _ = build_chat_payload("m", messages, 0.5)
    assert _roles(payload["messages"]) == ["system", "system", "user"]


def test_payload_merges_with_the_flag():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "snapshot"},
        {"role": "user", "content": "hello"},
    ]
    payload, _ = build_chat_payload("m", messages, 0.5, single_system=True)
    assert _roles(payload["messages"]) == ["system", "user"]
    assert payload["messages"][0]["content"].startswith("PROMPT")
    assert "snapshot" in payload["messages"][0]["content"]
    assert payload["messages"][1]["content"] == "hello"


def test_the_flag_does_not_alias_the_caller_list():
    messages = [{"role": "system", "content": "PROMPT"},
                {"role": "user", "content": "hello"}]
    payload, _ = build_chat_payload("m", messages, 0.5, single_system=True)
    assert payload["messages"] is not messages
