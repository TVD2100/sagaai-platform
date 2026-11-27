"""
Scenario: a multi-step dialog on a provider that keeps one system message.

A real agent dialog re-sends the whole conversation on every step, and the
platform journal changes on every step. Two things must hold at once for the
provider-side prefix cache to be worth anything:

- the opening bytes of the request never change, so the provider can match a
  cached prefix;
- every snapshot still reaches the model, so the agent keeps its context.

Before these rules existed the snapshots were merged INTO the prompt: the
first bytes changed on every step, every request started a fresh cache entry,
and the hit rate stayed at zero no matter how long the dialog ran.
"""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from core.api_chat import SNAPSHOT_MARKER, build_chat_payload

PROMPT = "# DevAgent - System Prompt\n\nYou are DevAgent."


def _journal(step, extra=""):
    """A task journal whose text grows from step to step."""
    return ("CURRENT TASK STATE:\n"
            f"### Step {step} - do the thing\n"
            f"- progress: {step}/9 steps done.{extra}")


def _dialog_step(step, history):
    """Assemble the message list of ONE dialog step.

    The platform prompt comes first, the hidden snapshots follow, then the
    turns accumulated so far, then the turn the user just sent.
    """
    messages = [{"role": "system", "content": PROMPT},
                {"role": "system", "content": _journal(step)}]
    messages.extend(history)
    messages.append({"role": "user", "content": f"go on, step {step}"})
    return messages


def test_the_opening_bytes_survive_a_growing_journal():
    """The prompt stays byte-identical while the journal grows.

    This is the precondition for a provider prefix cache: a cached prefix is
    matched by its bytes, so a single changed character at the front throws
    the whole prefix away.
    """
    heads = []
    history = []
    for step in range(1, 4):
        payload, _ = build_chat_payload("GigaChat/GigaChat-4-708B-A41",
                                        _dialog_step(step, history),
                                        0.2, single_system=True)
        heads.append(payload["messages"][0])
        history = history + [{"role": "user", "content": f"go on, step {step}"},
                             {"role": "assistant", "content": "done"}]

    assert len(heads) == 3
    # Every step opens with the same single system message, verbatim.
    assert all(h == {"role": "system", "content": PROMPT} for h in heads)
    assert len({h["content"] for h in heads}) == 1


def test_every_snapshot_still_reaches_the_model():
    """A stable head must not cost the agent its context.

    Each step's journal carries a fact the model has to see. Folding the
    snapshots into separate messages keeps them all in the request.
    """
    payload, _ = build_chat_payload("GigaChat/GigaChat-4-708B-A41",
                                    _dialog_step(7, [{"role": "user", "content": "hi"},
                                                     {"role": "assistant", "content": "hello"}]),
                                    0.2, single_system=True)
    wire = payload["messages"]
    # Exactly one system message - the provider keeps only one anyway.
    assert sum(1 for m in wire if m.get("role") == "system") == 1
    # The journal travels as a marked user message, intact.
    marked = [m for m in wire if SNAPSHOT_MARKER in (m.get("content") or "")]
    assert len(marked) == 1
    assert "### Step 7 - do the thing" in marked[0]["content"]
    assert "7/9 steps done" in marked[0]["content"]
    # The user's own turns are unchanged.
    user_texts = [m["content"] for m in wire if m.get("role") == "user"
                  and SNAPSHOT_MARKER not in (m.get("content") or "")]
    assert "hi" in user_texts
    assert "go on, step 7" in user_texts


def test_a_cached_answer_is_accounted_once():
    """The provider's cached tokens flow into the usage callback.

    A chat/completions gateway reports ``prompt_tokens_details.cached_tokens``
    separately from ``prompt_tokens``; the cache share is that number over the
    input total, and it must reach the accounting without being added twice.
    """
    seen = []
    from core.api_chat import report_chat_usage

    report_chat_usage(seen.append,
                      {"usage": {"prompt_tokens": 5444,
                                 "completion_tokens": 20,
                                 "prompt_tokens_details": {"cached_tokens": 5248}}},
                      [], "ok")
    assert seen == [{"in": 5444, "out": 20, "cache": 5248}]
    from core.statistics import cache_pct
    assert cache_pct(seen[0]["cache"], seen[0]["in"]) == 96
