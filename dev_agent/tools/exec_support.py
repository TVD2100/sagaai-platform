"""Helpers of the execution tools: script reading, redaction, park payloads.

``exec.py`` owns the execution path itself; everything that prepares its
inputs or shapes its outputs lives here, so the tool module stays small
enough for the structural budget to measure meaningfully.

Two helpers carry a security decision rather than a formatting one:

* ``read_script_text`` refuses binary and oversized files instead of decoding
  them - the analysed text must be exactly what the interpreter will see;
* ``redact_secrets`` removes credential-looking values from the output, since
  a tool result travels into the conversation and to the provider.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .. import config

#: Pattern of values that must never leave the process in a tool result.
_SECRET_VALUE_RE = re.compile(
    r"(?i)((?:api[_-]?key|token|secret|password|passwd|authorization)"
    r"\s*[:=]\s*)([^\s'\",;]{8,})"
)

#: A long opaque value next to no marker is still suspicious when it looks
#: like a provider key (a prefix followed by a long random body).
_SECRET_SHAPE_RE = re.compile(r"\b(?:sk|pk|ghp|gho|xoxb|AKIA|tvly)-[A-Za-z0-9_\-]{16,}")

#: Assignment to an environment-style NAME whose credential word sits at the
#: end (``SAGAAI_YANDEXAI_KEY=...``). The field pattern above looks for the
#: word immediately before the separator, so a name like this slips past it
#: although it carries exactly the value a full-access child can print.
_SECRET_ENV_RE = re.compile(
    r"(?im)(\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?))"
    r"[ \t]*[:=][ \t]*(\S{4,})"
)

#: Upper bound for a script read from ``path``. Larger files are refused:
#: analysing a huge blob is not useful and the caller gets a clear reason.
MAX_SCRIPT_BYTES = 512 * 1024


def redact_secrets(text: str) -> str:
    """Replace credential-looking values in *text* with a marker.

    The child process can print anything it can read; a printed key would
    otherwise travel into the conversation, into the history and to the model
    provider. The marker keeps the shape of the finding (which field leaked)
    without the value. Both the ``field=value`` form and the
    ``ENVIRONMENT_NAME=value`` form are covered, because a full-access child
    can dump the whole environment it received.
    """
    if not text:
        return text
    redacted = _SECRET_VALUE_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    redacted = _SECRET_ENV_RE.sub(lambda m: m.group(1) + "=[REDACTED]", redacted)
    return _SECRET_SHAPE_RE.sub("[REDACTED]", redacted)


def read_script_text(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return the text of a project script, or an error message.

    Binary content and oversized files are refused instead of being decoded:
    the analysed text must be exactly what the interpreter will see.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None, f"Script file not found: {path.name}"
    except OSError as exc:
        return None, f"Cannot read the script: {exc}"
    if size > MAX_SCRIPT_BYTES:
        return None, (
            f"The script is {size} bytes; the limit for an analysed script is "
            f"{MAX_SCRIPT_BYTES} bytes. Split it or run it through a test file."
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"Cannot read the script: {exc}"
    if b"\x00" in raw:
        return None, (
            "The file looks binary (NUL bytes). Analysing and running a binary "
            "file is refused; use a text script or a dedicated tool."
        )
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), None


def subject_for_analysis(code: Optional[str], path: Optional[str]) -> Tuple[str, str, Optional[Path]]:
    """Return the text to analyse, the label used in the dialog and the path.

    ``path`` mode is analysed through the file content, so the analysed text
    and the executed text are the same bytes.
    """
    if code is not None:
        return str(code), "", None
    target = config.resolve_in_project(path or "")
    text, error = read_script_text(target)
    if text is None:
        raise ValueError(error or "Cannot read the script")
    return text, text, target


def confirmation_result(tool: str, assessment, *, path: Optional[Path] = None,
                        code_hash: str = "") -> Dict[str, Any]:
    """Build the parked-run payload for a dangerous call."""
    payload = assessment.to_dict()
    if path is not None:
        payload["script_path"] = str(path)
    return {
        "ok": False,
        "confirmation_required": True,
        "tool": tool,
        "danger_assessment": payload,
        "code_hash": code_hash,
        "error": (
            "This execution requires user confirmation before it runs. "
            "The agent loop will stop and show a confirmation dialog."
        ),
    }


def inline_command(code: str, encoding: str):
    """Write *code* to a temp file; return ``(path to remove, argv)``."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding=encoding)
    try:
        handle.write(code)
        return handle.name, [__import__("sys").executable, handle.name]
    finally:
        handle.close()


def script_command(tool: str, target) -> list:
    """Return the argv that runs a project script or a pytest file."""
    executable = __import__("sys").executable
    if tool == "run_test":
        return [executable, "-m", "pytest", "-q", str(target)]
    return [executable, str(target)]


def subprocess_cwd() -> str:
    """Return the cwd for child processes spawned by run_test / run_code.

    In the empty state the neutral folder (created on demand) is used, so a
    child process can never inherit a neighbouring dialog's real project.
    Otherwise the active project root.
    """
    if not bool(getattr(config, "WORKSPACE_SELECTED", True)):
        try:
            return str(config.ensure_neutral_root())
        except OSError:
            pass
    return str(config.PROJECT_ROOT)


def result_payload(result: Dict[str, Any], tool: str, timeout_s: int,
                   code_hash: str) -> Dict[str, Any]:
    """Turn a child-process result into the tool result sent to the model."""
    from ..sandbox_proc import split_for_display

    out, out_cut = split_for_display(redact_secrets(result.get("stdout", "")))
    err, err_cut = split_for_display(redact_secrets(result.get("stderr", "")))
    payload: Dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "returncode": result.get("returncode"),
        "stdout": out,
        "stderr": err,
        "duration_ms": result.get("duration_ms", 0),
        "truncated": bool(result.get("truncated") or out_cut or err_cut),
        "log_path": result.get("log_path", ""),
        "code_hash": code_hash,
    }
    if result.get("timed_out"):
        payload["ok"] = False
        payload["timed_out"] = True
        payload["error"] = (
            f"{tool} timed out after {timeout_s}s. The whole process tree "
            "was stopped; the output above is what it managed to print."
        )
    return payload


def script_log_path(tool: str) -> Path:
    """Return a per-run log path under the platform runtime directory."""
    base = Path(tempfile.gettempdir()) / "sagaai-logs"
    return base / f"{tool}-{os.getpid()}-{id(object())}.log"

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
