"""
core.context_guard - pre-flight context-window protection for api_layer (M4/M5).

Before a model call, estimate the outgoing input payload and either drop the
oldest history messages until the total fits under half the context window
(soft trim), or raise ContextWindowError when the payload cannot fit under
0.8 of the window even with an empty history. Passthrough when the window is
unknown. Never mutates the caller's history - a new list is returned.
"""

from core.api_errors import ContextWindowError
from core.files import estimate_tokens, get_model_context_window


def apply_context_guard(hist_msgs, user_content, assistant=None, max_tokens=0,
                        svc_name='', services=None):
    """Trim *hist_msgs* from the front until the payload fits the window.

    Returns a NEW list. Passthrough when the model's context window cannot
    be determined (window <= 0) or the lookup fails. Raises
    ContextWindowError when the hard threshold (0.8 of the window) cannot be
    met even after dropping every history message.
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
    # Reserve part of the window for the generated output, but never more
    # than half of it: providers may declare max output tokens larger than
    # the context window itself (e.g. DeepSeek 384k vs 128k window).
    fixed += min(int(max_tokens or 0), int(window * 0.5))
    soft = int(window * 0.5)
    hard = int(window * 0.8)
    trimmed = list(hist_msgs)
    while trimmed and fixed + _estimate_messages(trimmed) > soft:
        trimmed.pop(0)
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
