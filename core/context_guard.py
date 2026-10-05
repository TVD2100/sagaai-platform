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
"""

from core.api_errors import ContextWindowError
from core.files import estimate_tokens, get_model_context_window

_MIN_OUTPUT_RESERVE = 4096
_OUTPUT_RESERVE_RATIO = 0.25
_SOFT_RATIO = 0.5
_HARD_RATIO = 0.8

# Head block that must survive trimming (see the module docstring).
_ECONOMY_META_MARKER = "ECONOMY MODE: ENABLED"


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
    """Return the effective history budget the guard allows (0 when none).

    Mirrors ``apply_context_guard``'s steady state: the soft threshold
    ``int(window * _SOFT_RATIO)`` minus the output reserve
    (``_output_reserve``) minus the fixed prompt estimate. Callers that
    build their own window (the economy mode) keep their budget at or below
    this value so the guard never has to trim their payload and the sent
    prefix stays stable. Returns 0 when *window* is not positive.
    """
    try:
        window = int(window or 0)
    except Exception:
        return 0
    if window <= 0:
        return 0
    try:
        prompt = max(0, int(prompt_tokens or 0))
    except Exception:
        prompt = 0
    cap = int(window * _SOFT_RATIO) - _output_reserve(window, max_tokens) - prompt
    return max(0, cap)


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
