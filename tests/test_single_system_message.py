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


def test_a_second_system_becomes_a_marked_user_message():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "CURRENT TASK STATE: x"},
        {"role": "user", "content": "hello"},
    ]
    out = collapse_system_messages(messages)
    assert _roles(out) == ["system", "user", "user"]
    # The prompt is untouched, so the opening bytes stay the same on every
    # request and a prefix cache can reuse them.
    assert out[0] == {"role": "system", "content": "PROMPT"}
    assert out[1]["content"].startswith(SNAPSHOT_MARKER)
    assert "CURRENT TASK STATE: x" in out[1]["content"]
    assert out[2] == {"role": "user", "content": "hello"}


def test_snapshots_keep_their_arrival_order():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "SNAP-1"},
        {"role": "system", "content": "SNAP-2"},
        {"role": "user", "content": "hello"},
    ]
    out = collapse_system_messages(messages)
    assert _roles(out) == ["system", "user", "user", "user"]
    assert "SNAP-1" in out[1]["content"]
    assert "SNAP-2" in out[2]["content"]


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


def test_a_late_system_message_keeps_its_place():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "user", "content": "first"},
        {"role": "system", "content": "late snapshot"},
        {"role": "assistant", "content": "answer"},
    ]
    out = collapse_system_messages(messages)
    # Chronology is preserved: the snapshot stays between the turns it
    # actually sat between.
    assert _roles(out) == ["system", "user", "user", "assistant"]
    assert out[1]["content"] == "first"
    assert "late snapshot" in out[2]["content"]
    assert out[3]["content"] == "answer"


def test_trailing_snapshot_stays_trailing():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "user", "content": "hello"},
        {"role": "system", "content": "trailing"},
    ]
    out = collapse_system_messages(messages)
    assert _roles(out) == ["system", "user", "user"]
    assert out[0] == {"role": "system", "content": "PROMPT"}
    assert "trailing" in out[2]["content"]


def test_no_user_message_at_all_still_yields_one_system():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "snapshot"},
    ]
    out = collapse_system_messages(messages)
    assert _roles(out) == ["system", "user"]
    assert out[0] == {"role": "system", "content": "PROMPT"}
    assert "snapshot" in out[1]["content"]


def test_the_head_is_byte_identical_when_a_snapshot_changes():
    # The point of moving snapshots out of the prompt: a prefix cache reuses
    # the opening bytes only while they do not change. Two consecutive steps
    # with a DIFFERENT journal must still produce the same leading bytes.
    step_one = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "CURRENT TASK STATE: step 1"},
        {"role": "user", "content": "go"},
    ]
    step_two = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "CURRENT TASK STATE: step 2, much longer now"},
        {"role": "user", "content": "go"},
    ]
    first = collapse_system_messages(step_one)
    second = collapse_system_messages(step_two)
    assert first[0] == second[0]
    assert first[0]["content"] == "PROMPT"
    # The snapshot text itself still differs - only the head is stable.
    assert first[1]["content"] != second[1]["content"]


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


# ─── the shipped catalogs match the measured behaviour ──────────────────────
# Collapsing is a lossy workaround, so the catalog may claim it only for the
# models that really keep a single system message. A wider claim silently
# degrades every other model of the service: its prompt gets merged with the
# platform snapshots and loses the byte stability that makes a prefix cache
# reusable.


def _service(name):
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    return json.loads((root / "services" / name).read_text(encoding="utf-8"))


def test_cloud_collapses_the_whole_gigachat_line():
    svc = _service("cloud.json")
    for model in ("GigaChat/GigaChat-4-708B-A41", "GigaChat/GigaChat-3-Pro",
                  "ai-sage/GigaChat3.5-432B-A28B", "ai-sage/GigaChat3-10B-A1.8B"):
        assert needs_single_system(svc, model) is True, model
    for model in ("openai/gpt-5.4", "openai/gpt-5.4-mini",
                  "anthropic/claude-opus-4.8", "anthropic/claude-sonnet-4.6",
                  "google/gemini-2.5-flash", "meta-llama/llama-3.3-70b-instruct"):
        assert needs_single_system(svc, model) is False, model


def test_ai_studio_collapses_every_chat_model():
    svc = _service("gigachat_ai_studio.json")
    for model in ("GigaChat-4-708B-A41B", "GigaChat-3.5-432B-A28B",
                  "GigaChat-3-Ultra", "GigaChat-3-Lightning"):
        assert needs_single_system(svc, model) is True, model


# ─── build_chat_payload ─────────────────────────────────────────────────────

def test_payload_keeps_every_system_without_the_flag():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "snapshot"},
        {"role": "user", "content": "hello"},
    ]
    payload, _ = build_chat_payload("m", messages, 0.5)
    assert _roles(payload["messages"]) == ["system", "system", "user"]


def test_payload_keeps_one_system_with_the_flag():
    messages = [
        {"role": "system", "content": "PROMPT"},
        {"role": "system", "content": "snapshot"},
        {"role": "user", "content": "hello"},
    ]
    payload, _ = build_chat_payload("m", messages, 0.5, single_system=True)
    wire = payload["messages"]
    assert _roles(wire).count("system") == 1
    assert wire[0] == {"role": "system", "content": "PROMPT"}
    assert SNAPSHOT_MARKER in wire[1]["content"]
    assert wire[-1] == {"role": "user", "content": "hello"}


def test_the_flag_does_not_alias_the_caller_list():
    messages = [{"role": "system", "content": "PROMPT"},
                {"role": "user", "content": "hello"}]
    payload, _ = build_chat_payload("m", messages, 0.5, single_system=True)
    assert payload["messages"] is not messages
