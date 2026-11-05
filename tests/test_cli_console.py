"""Tests of the console output layer.

Three properties decide whether a script can rely on the console:

- the JSON mode writes exactly ONE object on stdout and nothing else;
- the streams do not mix - progress goes to stderr, so redirecting it away
  keeps the result readable;
- the exit code travels inside the object too, so a caller that only reads
  stdout still learns the outcome.

The human mode is checked for the opposite reason: it must stay readable as a
message, which means a status marker, an aligned table and no JSON noise.
"""

import io
import json

from sagaai.console import Console, human_bytes, human_seconds
from sagaai.errors import CliError, config_error, conflict_error

# ─── JSON envelope ──────────────────────────────────────────────────────────


def _console(json_mode=True):
    out, err = io.StringIO(), io.StringIO()
    return Console(json_mode=json_mode, stream=out, err_stream=err), out, err


def test_the_json_mode_prints_one_object_with_the_documented_keys():
    console, out, _err = _console(True)
    console.emit("doctor", {"status": "ok"}, exit_code=0, version="9.9.9")
    payload = json.loads(out.getvalue())
    assert set(payload) == {"ok", "command", "exit_code", "version", "data"}
    assert payload["ok"] is True
    assert payload["command"] == "doctor"
    assert payload["exit_code"] == 0
    assert payload["version"] == "9.9.9"
    assert payload["data"] == {"status": "ok"}


def test_the_progress_notes_never_reach_stdout_in_json_mode():
    console, out, err = _console(True)
    console.note("working...")
    console.emit("run", {"status": "done"}, exit_code=0, version="1.0")
    assert "working" not in out.getvalue()
    assert out.getvalue().strip().startswith("{")
    json.loads(out.getvalue())


def test_an_error_in_json_mode_carries_the_code_and_the_hint():
    console, out, _err = _console(True)
    error = config_error("api_disabled", "the API is off", hint="enable it")
    console.emit_error("serve", error, exit_code=error.exit_code, version="1.0")
    payload = json.loads(out.getvalue())
    assert payload["ok"] is False
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "api_disabled"
    assert payload["error"]["message"] == "the API is off"
    assert payload["error"]["hint"] == "enable it"
    assert "data" not in payload


def test_the_error_object_omits_an_empty_hint():
    console, out, _err = _console(True)
    console.emit_error("doctor", CliError("failure", "boom"), exit_code=1)
    payload = json.loads(out.getvalue())
    assert "hint" not in payload["error"]


def test_non_ascii_text_survives_the_json_round_trip():
    console, out, _err = _console(True)
    console.emit("run", {"summary": "Готово: 3 файла"})
    payload = json.loads(out.getvalue())
    assert payload["data"]["summary"] == "Готово: 3 файла"


# ─── Human mode ─────────────────────────────────────────────────────────────


def test_the_human_mode_renders_a_status_row_with_a_marker():
    console, out, _err = _console(False)
    console.title("checks")
    console.status("databases", "warn", "not initialized")
    console.emit("doctor", {}, exit_code=0)
    text = out.getvalue()
    assert "[warn]" in text
    assert "databases" in text
    assert "not initialized" in text
    assert "{" not in text


def test_every_known_status_has_a_marker():
    for status in ("ok", "warn", "error", "skipped"):
        console, out, _err = _console(False)
        console.status("probe", status, "detail")
        console.emit("doctor", {})
        assert "[????]" not in out.getvalue(), status


def test_a_table_aligns_its_columns():
    console, out, _err = _console(False)
    console.table(["id", "scope"], [["abc", "dev_agent"], ["d", "*"]])
    console.emit("token list", {})
    lines = out.getvalue().splitlines()
    header = next(line for line in lines if "id" in line and "scope" in line)
    dev_row = next(line for line in lines if "dev_agent" in line)
    # The second column starts at the same offset in both rows: that is what
    # "aligned" means, and a widening first column is the only way it breaks.
    assert header.index("scope") == dev_row.index("dev_agent")
    assert "*" in out.getvalue()


def test_a_field_renders_booleans_and_lists_readably():
    console, out, _err = _console(False)
    console.field("enabled", True)
    console.field("slugs", ["a", "b"])
    console.field("missing", None)
    console.emit("doctor", {})
    text = out.getvalue()
    assert "yes" in text and "no" not in text.split("slugs")[0]
    assert "a, b" in text
    assert "-" in text


def test_a_warning_goes_to_stderr_in_both_modes():
    for mode in (True, False):
        console, out, err = _console(mode)
        console.warn("be careful")
        console.emit("doctor", {})
        assert "be careful" in err.getvalue()
        assert "be careful" not in out.getvalue()


# ─── Error construction ─────────────────────────────────────────────────────


def test_the_three_constructors_carry_the_documented_codes():
    assert config_error("x", "y").exit_code == 2
    assert failure_code() == 1
    assert conflict_error("x", "y").exit_code == 3


def failure_code():
    from sagaai.errors import failure
    return failure("x", "y").exit_code


# ─── Human formatters ───────────────────────────────────────────────────────


def test_human_bytes_uses_binary_units():
    assert human_bytes(0) == "0 B"
    assert human_bytes(512) == "512 B"
    assert human_bytes(2048) == "2.0 KB"
    assert human_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_human_seconds_splits_minutes_and_hours():
    assert human_seconds(5) == "5s"
    assert human_seconds(65) == "1m 5s"
    assert human_seconds(3660) == "1h 1m"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
