"""
core.context_guard - pre-flight context-window protection for api_layer (M4/M5).

Before a model call, estimate the outgoing input payload and either drop the
oldest history messages until the total fits under ``_SOFT_RATIO`` of the
context window (soft trim), or raise ContextWindowError when the payload
cannot fit under ``_HARD_RATIO`` of the window even with an empty history.
Passthrough when the window is unknown. Never mutates the caller's history -
a new list is returned.

The output reserve (tokens held back for the generated answer) is
``min(max_out, max(4096, 0.25 * window))``: a provider-declared output limit
larger than the window cannot inflate the reserve, and small windows keep the
4096-token floor. The economy-mode metadata block, when present as the head
element of the payload, is never trimmed: dropping it would lose the economy
notice for the model and break the cached prefix on every saturated step.

``history_cap`` exposes the guard's effective history budget (soft threshold
minus the reserve minus the fixed prompt). Callers that build their own
window (the economy mode in ``dev_agent.agent_loop``) use it to stay BELOW
the guard, so the stateless front trim in this module stays a safety net
instead of sliding the cached prefix on every step.

``input_ceiling`` / ``input_budget`` expose the UNIFIED input budget shared
with the economy mode: the input ceiling is the context window minus the
model's declared output limit (the provider must hold the request and the
requested answer together), and the input budget keeps ``_INPUT_BUDGET_RATIO``
of that ceiling for the payload. The soft threshold was raised to
``_SOFT_RATIO`` = 0.75 and the hard cap to ``_HARD_RATIO`` = 0.9 so a history
built on the input budget fits BELOW the guard and is never slid by it.
"""

from core.api_errors import ContextWindowError
from core.files import estimate_tokens, get_model_context_window

_MIN_OUTPUT_RESERVE = 4096
_OUTPUT_RESERVE_RATIO = 0.25
# Unified input budget shared with the economy mode (dev_agent.agent_loop):
# the payload may occupy _INPUT_BUDGET_RATIO of (window - declared output
# limit). The guard's soft trim sits ABOVE that ratio and the hard cap above
# both, so an economy-built payload is never slid (see module docstring).
_INPUT_BUDGET_RATIO = 0.75
_SOFT_RATIO = 0.75
_HARD_RATIO = 0.9

# Head block that must survive trimming (see the module docstring).
_ECONOMY_META_MARKER = "ECONOMY MODE: ENABLED"


def input_ceiling(window, max_tokens=0) -> int:
    """Return the unified input ceiling: ``window - declared output limit``.

    The provider must hold the request and the requested answer inside the
    context window, so the tokens available for the input payload are the
    window minus the model's declared output limit. Returns 0 when the
    window is not positive.
    """
    try:
        window = int(window or 0)
    except Exception:
        return 0
    if window <= 0:
        return 0
    try:
        declared = max(0, int(max_tokens or 0))
    except Exception:
        declared = 0
    return max(0, window - declared)


def input_budget(window, max_tokens=0, prompt_tokens=0) -> int:
    """Return the unified history budget: ``ratio * ceiling - prompt``.

    The economy mode and the guard share this budget: the payload (history)
    may occupy ``_INPUT_BUDGET_RATIO`` of the input ceiling, minus the fixed
    prompt estimate. Returns 0 when the window is not positive.
    """
    ceiling = input_ceiling(window, max_tokens)
    if ceiling <= 0:
        return 0
    try:
        prompt = max(0, int(prompt_tokens or 0))
    except Exception:
        prompt = 0
    return max(0, int(ceiling * _INPUT_BUDGET_RATIO) - prompt)


def _output_reserve(window, max_tokens) -> int:
    """Return the output reserve: ``min(max_out, max(4096, 0.25 * window))``.

    A falsy *max_tokens* (no declared output limit) keeps the previous
    behaviour: no reserve is subtracted from the history budget.
    """
    try:
        want = max(_MIN_OUTPUT_RESERVE, int(int(window) * _OUTPUT_RESERVE_RATIO))
    except Exception:
        want = _MIN_OUTPUT_RESERVE
    try:
        declared = max(0, int(max_tokens or 0))
    except Exception:
        declared = 0
    return min(declared, want)


def history_cap(window, max_tokens=0, prompt_tokens=0) -> int:
    """Return the effective history budget (0 when none).

    Delegates to :func:`input_budget`: the shared economy/guard budget is
    ``_INPUT_BUDGET_RATIO`` of the input ceiling (window minus the declared
    output limit) minus the fixed prompt estimate. Because the input budget
    is kept BELOW ``apply_context_guard``'s soft threshold, the stateless
    guard trim stays a safety net and never slides a cached prefix. Returns
    0 when *window* is not positive.
    """
    return input_budget(window, max_tokens, prompt_tokens)


def _head_keep_count(hist_msgs) -> int:
    """Return 1 when the payload head is the economy metadata block, else 0."""
    if not hist_msgs:
        return 0
    first = hist_msgs[0]
    content = first.get("content", "") if isinstance(first, dict) else ""
    if isinstance(content, str) and content.startswith(_ECONOMY_META_MARKER):
        return 1
    return 0


def apply_context_guard(hist_msgs, user_content, assistant=None, max_tokens=0,
                        svc_name='', services=None):
    """Trim *hist_msgs* from the front until the payload fits the window.

    Returns a NEW list. Passthrough when the model's context window cannot
    be determined (window <= 0) or the lookup fails. Raises
    ContextWindowError when the hard threshold (``_HARD_RATIO`` of the
    window) cannot be met even after dropping every trimmable history
    message. The economy metadata block (the head element), when present,
    is never dropped.
    """
    assistant = assistant or {}
    try:
        window = int(get_model_context_window(assistant, services or {}) or 0)
    except Exception:
        window = 0
    if window <= 0:
        return hist_msgs
    fixed = estimate_tokens(str(assistant.get('text', '') or ''))
    fixed += estimate_tokens(user_content or '')
    fixed += _output_reserve(window, max_tokens)
    soft = int(window * _SOFT_RATIO)
    hard = int(window * _HARD_RATIO)
    keep = _head_keep_count(hist_msgs)
    trimmed = list(hist_msgs or [])
    while len(trimmed) > keep and fixed + _estimate_messages(trimmed) > soft:
        trimmed.pop(keep)
    total = fixed + _estimate_messages(trimmed)
    if total > hard:
        raise ContextWindowError(
            window=window, tokens=total, max_output=max_tokens, service=svc_name)
    return trimmed


def _estimate_messages(messages):
    """Estimate input tokens for a list of API-style message dicts."""
    total = 0
    for msg in messages:
        content = msg.get('content', '') if isinstance(msg, dict) else ''
        if isinstance(content, str):
            total += estimate_tokens(content)
    return max(1, total)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
