"""
sagaai run - give a task to an employee without opening the interface.

The command is the console face of the same launch the page and the HTTP API
perform: it cold-starts the installation, builds a ``DialogRequest``, and
calls ``core.gateway.dialog.run_dialog``, which starts the run through the
gateway and blocks until it ends. Nothing about the agent loop is
duplicated here - a task started from the console is the same run the
interface would have started, with the same thread, the same journal and the
same limits.

The waiting has three outcomes and they are NOT the same thing:

- the run finished: its own status (done / failed / cancelled) decides the
  exit code, because the command succeeded in running the task;
- the run paused: a plan needs approval or a question needs an answer. That
  is a RESULT, not a failure - the report names the pause and the run id, and
  the exit code stays 0 so a script can decide what to do with the answer;
- the wait expired: the run is still working. Exit code 3 tells a script
  "ask again later", and the run id in the report is how it asks. The run is
  NOT cancelled - giving up on waiting must not destroy work.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List

from sagaai import exit_codes
from sagaai.commands import _common
from sagaai.console import human_seconds
from sagaai.errors import CliError, config_error

SUMMARY = "give a task to an employee and wait for the result"
DESCRIPTION = (
    "Runs one task with the named employee, exactly as the interface or the "
    "HTTP API would, and waits for it to finish. The task is stored as a "
    "normal dialog, so it appears in History afterwards. ``--json`` prints the "
    "run id, the status, the summary, the tokens spent and the final message."
)

#: How long to wait by default. A root run has its own deadline; this one is
#: how long a CONSOLE session is willing to stay attached to it.
DEFAULT_TIMEOUT = 3600


def register(parser: argparse.ArgumentParser, add_global_flags) -> None:
    """Declare the arguments of ``sagaai run``."""
    parser.add_argument("--agent", required=True, metavar="SLUG",
                        help="slug of the employee (see: sagaai agents)")
    parser.add_argument("--task", required=True, metavar="TEXT",
                        help="the task, in the employee's own terms")
    parser.add_argument("--workspace", default="", metavar="PATH",
                        help="project folder the employee works in "
                             "(absolute path)")
    parser.add_argument("--max-steps", type=int, default=0, dest="max_steps",
                        help="step ceiling for this run (default: the "
                             "employee's own limit)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="how long to wait for the result in seconds "
                             "(default: %d)" % DEFAULT_TIMEOUT)
    add_global_flags(parser)


def _request(args: argparse.Namespace):
    """Build the dialog request from the parsed arguments."""
    from core.gateway.dto import DialogRequest

    workspace = str(getattr(args, "workspace", "") or "").strip()
    if workspace:
        workspace = os.path.abspath(os.path.expanduser(workspace))
        if not os.path.isdir(workspace):
            raise config_error("workspace_not_found",
                               "no such folder: " + workspace,
                               hint="--workspace takes an existing absolute path")
    request = DialogRequest(
        agent_slug=str(getattr(args, "agent", "") or "").strip(),
        message=str(getattr(args, "task", "") or "").strip(),
        workspace=workspace,
        mode="auto",
        max_steps=max(0, int(getattr(args, "max_steps", 0) or 0)),
    )
    problem = request.validate()
    if problem:
        raise config_error("invalid_request", problem)
    return request


def _messages_brief(outcome: Any) -> List[Dict[str, Any]]:
    """Return the last few messages in a shape a script can read."""
    items: List[Dict[str, Any]] = []
    for message in list(getattr(outcome, "messages", None) or [])[-6:]:
        to_dict = getattr(message, "to_dict", None)
        if callable(to_dict):
            items.append(to_dict())
    return items


def _exit_code(outcome: Any) -> int:
    """Translate the outcome of a run into the documented exit code."""
    if getattr(outcome, "timed_out", False):
        return exit_codes.CONFLICT
    status = str(getattr(outcome, "status", "") or "")
    if status == "done":
        return exit_codes.OK
    if status in ("failed", "cancelled"):
        return exit_codes.FAILURE
    if status in ("awaiting_input", "awaiting_user", "awaiting_approval"):
        # A pause is a result: the run did what it could without a human.
        return exit_codes.OK
    return exit_codes.FAILURE


def execute(args: argparse.Namespace, console) -> int:
    """Start the task, wait for it and report the outcome."""
    from core.gateway import bootstrap, dialog

    started = _common.call(bootstrap.cold_start)
    failed = list(started.get("failed") or [])
    if failed:
        raise config_error(
            "bootstrap_failed",
            "the installation could not be prepared: " + ", ".join(failed),
            hint="run: sagaai doctor --verbose",
        )

    request = _request(args)
    timeout = max(1, int(getattr(args, "timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT))
    console.note("running %s, waiting up to %s" % (
        request.agent_slug, human_seconds(timeout)))

    outcome = _common.call(dialog.run_dialog, request, wait_seconds=timeout)
    exit_code = _exit_code(outcome)
    data: Dict[str, Any] = {
        "run_id": outcome.run_id,
        "thread_id": outcome.thread_id,
        "agent_slug": outcome.agent_slug,
        "status": outcome.status,
        "stop_reason": outcome.stop_reason,
        "error_code": outcome.error_code,
        "summary": outcome.summary,
        "steps_used": outcome.steps_used,
        "tokens_used": dict(outcome.tokens_used or {}),
        "artifacts": list(outcome.artifacts or []),
        "pending_input": (outcome.pending_input.to_dict()
                          if outcome.pending_input else None),
        "messages": _messages_brief(outcome),
        "timed_out": bool(outcome.timed_out),
        "elapsed_seconds": float(outcome.elapsed_seconds or 0.0),
    }
    if not console.json_mode:
        console.title("task result")
        console.field("employee", data["agent_slug"])
        console.field("status", data["status"])
        console.field("run id", data["run_id"])
        console.field("dialog id", data["thread_id"])
        console.field("steps", data["steps_used"])
        tokens = data["tokens_used"]
        if tokens:
            console.field("tokens", "in %s / out %s" % (
                tokens.get("in", 0), tokens.get("out", 0)))
        console.field("elapsed", human_seconds(data["elapsed_seconds"]))
        if data["artifacts"]:
            console.blank()
            console.title("files")
            for item in data["artifacts"]:
                console.bullet(str(item.get("path") or item))
        if data["summary"]:
            console.blank()
            console.title("summary")
            _render_text(console, str(data["summary"]))
        pending = data["pending_input"]
        if pending:
            console.blank()
            console.title("the run is paused")
            console.field("kind", pending.get("kind"))
            if pending.get("tool"):
                console.field("tool", pending.get("tool"))
            question = pending.get("question") or {}
            if question.get("text"):
                _render_text(console, str(question.get("text")))
            if pending.get("plan"):
                _render_text(console, str(pending.get("plan")))
        if data["timed_out"]:
            console.blank()
            console.warn("the wait expired; the run is still working")
            console.line("  follow it with the run id above")
    console.emit("run", data, exit_code=exit_codes.OK
                 if exit_code == exit_codes.OK else exit_code,
                 version=_common.platform_version())
    if exit_code == exit_codes.CONFLICT:
        # The report above is the ANSWER: a timeout is a normal outcome a
        # script reads from ``timed_out``, not a printed error block.
        return exit_codes.CONFLICT
    if exit_code == exit_codes.FAILURE:
        raise CliError("run_failed",
                       "the task finished as %s" % (data["status"] or "failed"),
                       exit_code=exit_codes.FAILURE,
                       hint="the dialog is stored: open it in History, or "
                            "check sagaai doctor")
    return exit_code


def _render_text(console, text: str, limit: int = 4000) -> None:
    """Print a block of text indented, capped so a terminal stays usable."""
    body = str(text or "")
    if len(body) > limit:
        body = body[:limit].rstrip() + "\n  ... (truncated)"
    for line in body.splitlines() or [""]:
        console.line("  " + line)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
