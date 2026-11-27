# -*- coding: utf-8 -*-
"""tests.test_wire_messages - history messages lose platform bookkeeping.

A history entry is a UI record: it carries a timestamp, an absolute index
and rendered events so the dialog can be redrawn and replayed. A
chat/completions provider accepts only role/content/name/tool_calls/
tool_call_id, and a strict gateway rejects an unknown key instead of
ignoring it. ``_wire_messages`` builds the outgoing copy.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


BOOKKEEPING = {"ts": "2026-01-01T00:00:00", "_index": 1, "_events": [{"type": "error"}],
               "hidden": True}


def test_platform_bookkeeping_is_dropped():
    """Given a decorated history entry, when it is put on the wire, then
    only the fields a provider understands survive."""
    from core.api_payload import _wire_messages

    wire = _wire_messages([dict({"role": "user", "content": "a"}, **BOOKKEEPING)])
    assert wire == [{"role": "user", "content": "a"}]


def test_provider_fields_are_kept():
    """Given an entry carrying tool-call fields, when it is put on the wire,
    then those fields reach the provider untouched."""
    from core.api_payload import _wire_messages

    tool_call = {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [tool_call], "ts": "t"},
        {"role": "tool", "content": "result", "tool_call_id": "c1", "name": "read_file"},
    ]
    wire = _wire_messages(messages)
    assert wire[0] == {"role": "assistant", "content": "", "tool_calls": [tool_call]}
    assert wire[1] == {"role": "tool", "content": "result",
                       "tool_call_id": "c1", "name": "read_file"}


def test_helpers_tolerate_empty_and_foreign_entries():
    """Given no history or a non-dict entry, when the wire copy is built,
    then it degrades instead of raising."""
    from core.api_payload import _wire_messages

    assert _wire_messages([]) == []
    assert _wire_messages(None) == []
    assert _wire_messages(["raw", 42]) == ["raw", 42]


def test_bearer_request_sends_only_provider_fields():
    """Given a Cloud employee with a decorated dialog, when a turn is sent,
    then the request body carries no platform bookkeeping."""
    from core.api_layer import send_request

    svc = json.loads((ROOT / "services" / "cloud.json").read_text(encoding="utf-8"))
    captured = {}

    def _fake_post(url, **kwargs):
        captured["json"] = kwargs["json"]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        return resp

    with patch("core.api_layer.get_services", return_value={"Cloud": svc}), \
         patch("core.api_layer.load_config", return_value={"CLOUD_API_KEY": "k"}), \
         patch("core.api_layer.load_assistant_files_context", return_value=""), \
         patch("core.api_layer.requests.post", _fake_post):
        send_request(
            "hi",
            {"service": "Cloud", "model": "openai/gpt-5.4",
             "temperature": 0.3, "text": "SYS", "id": "x"},
            history=[dict({"role": "user", "content": "a"}, **BOOKKEEPING)],
        )

    for message in captured["json"]["messages"]:
        assert set(message) == {"role", "content"}, message

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
