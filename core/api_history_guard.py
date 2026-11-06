"""
core.api_history_guard - sanitising the conversation history before a call.

Model requests carry prior turns back to the provider, including tool results
that originated outside the platform. Those payloads may hide prompt-injection
instructions, so this module fences them as data and, when a signature is
detected, withholds the original text behind a sanitized marker.
"""
import json

from core.prompt_guard import (
    detect_injection_signatures,
    is_tool_result_text,
    sanitize_text,
    sanitize_tool_result_content,
)

# ─── Sanitized-info helper ──────────────────────────────────────────────────
_SANITIZED_REASON = "[SANITIZED: potential prompt-injection signature detected; original content withheld]"


def _parse_sanitized_info(tool_result_text: str) -> dict:
    """Return a compact info dict describing a sanitized tool-result payload.

    The payload is a serialized ``{"tool_result": {...}}`` envelope. We
    extract the tool name and the file path (if present) so the caller/UI can
    show the user exactly which tool/result was withheld.
    """
    info: dict = {"reason": _SANITIZED_REASON}
    try:
        data = json.loads(tool_result_text)
        tr = data.get("tool_result", {})
        if isinstance(tr, dict):
            tname = tr.get("tool", "") or tr.get("name", "")
            if tname:
                info["tool"] = str(tname)
            path = tr.get("path", "")
            if path:
                info["path"] = str(path)
            err = tr.get("error", "")
            if err:
                info["error"] = str(err)
    except Exception:
        pass
    return info


def _protect_history(hist_msgs: list,
                     enable_injection_protection: bool = True,
                     sanitized_callback=None,
                     approved_paths=None) -> list:
    """Return a copy of *hist_msgs* with tool-result payloads sanitized.

    Messages that look like serialized tool results (content starting with
    `{"tool_result"`) have their content passed through
    ``sanitize_tool_result_content``: control characters are removed, the
    payload is wrapped in [DATA_BEGIN / DATA_END] fences.

    When *enable_injection_protection* is True (default), injection-signature
    matches cause the payload to be replaced by a short [SANITIZED]
    placeholder.  When False, the content is still scrubbed of control
    characters and wrapped in data fences, but the original text is
    preserved (the ``strict`` flag is False).

    When a payload is actually replaced by the [SANITIZED] placeholder,
    *sanitized_callback* (if provided) is called with a compact info dict:
    {"reason", "tool"?, "path"?, "error"?}. This lets the caller detect
    sanitization and, for example, stop the loop for user approval.

    *approved_paths* may be provided as an iterable of file path strings.
    Tool-result messages whose embedded "path" is in that set have been
    explicitly approved by the user and are wrapped with data fences but
    NOT re-sanitized (and therefore do not re-trigger the callback), even
    when protection is enabled.

    Non-dict messages and regular user/assistant text are returned
    unchanged (the same object, not copied) to keep the overhead minimal.
    """
    if not hist_msgs:
        return hist_msgs or []

    approved_paths = set(approved_paths or ())
    protected: list = []
    for i, msg in enumerate(hist_msgs):
        if not isinstance(msg, dict):
            protected.append(msg)
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and is_tool_result_text(content):
            m = dict(msg)
            info = _parse_sanitized_info(content)
            path = info.get("path", "")
            strict = enable_injection_protection and path not in approved_paths

            # Determine whether this payload will actually be replaced by the
            # [SANITIZED] placeholder BEFORE sanitizing, rather than checking
            # for the literal "[SANITIZED" substring in the result. The raw
            # file/tool content may legitimately contain that literal (e.g.
            # core/api_layer.py itself), which would otherwise cause a false
            # positive and stop the loop even when protection is disabled.
            will_sanitize = strict and bool(
                detect_injection_signatures(sanitize_text(content))
            )

            new_content = sanitize_tool_result_content(content, source="tool_result",
                                                       strict=strict)
            if will_sanitize and sanitized_callback is not None:
                info = _parse_sanitized_info(content)
                info["message_index"] = i
                try:
                    sanitized_callback(info)
                except Exception:
                    pass
            m["content"] = new_content
            protected.append(m)
        else:
            protected.append(msg)
    return protected

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
