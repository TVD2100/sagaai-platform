"""
core.api_reasoning - removing chain-of-thought from an answer text.

A thinking model may put its reasoning into the visible answer instead of a
dedicated field: the reply arrives wrapped in think tags, and the model does
not have to close such a tag - it opens a new one for the next reasoning
block, so one message alternates reasoning and payload several times.

Both the transport layer (core.api_responses) and the chat feed
(ui.pages.orchestrator_text) must show only the answer, so the rule lives here
and both call it. Reasoning is internal: it must never reach the user, be fed
to the tool-call parser, or be persisted as part of an answer.
"""
import re

#: Names a thinking model uses for the tags around its chain-of-thought.
_REASONING_ALT = "think|thinking|thought|reasoning"

#: An opening tag of any reasoning dialect: ``<think>``, ``<thinking ...>``.
_REASONING_OPEN_RE = re.compile(
    r"<\s*(?:" + _REASONING_ALT + r")\b[^<>]*>", re.IGNORECASE)

#: A closed reasoning block, from its opening tag to its closing one.
_REASONING_PAIR_RE = re.compile(
    r"<\s*(?:" + _REASONING_ALT + r")\b[^<>]*>.*?"
    r"<\s*/\s*(?:" + _REASONING_ALT + r")\s*>",
    re.IGNORECASE | re.DOTALL)

#: A fenced payload: where the model resumes with real output after reasoning.
_PAYLOAD_FENCE = "```"


def strip_reasoning(text: str) -> str:
    """Return *text* without the chain-of-thought inlined into it.

    Closed blocks are removed whole. An opener that is never closed marks
    reasoning that runs up to the tool payload of the same message, so that
    prefix goes with it and the payload survives. When no payload follows,
    the block is an unfinished answer: everything from the opener on is
    reasoning, so only the prose before it stays. A real answer is therefore
    never truncated - prose always precedes the reasoning that follows it.
    """
    if not text:
        return text
    text = _REASONING_PAIR_RE.sub("", text)
    while True:
        opener = _REASONING_OPEN_RE.search(text)
        if opener is None:
            return text
        fence = text.find(_PAYLOAD_FENCE, opener.end())
        if fence < 0:
            return text[:opener.start()].strip()
        text = text[:opener.start()] + text[fence:]


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
