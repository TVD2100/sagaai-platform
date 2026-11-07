"""Child processes: a test file or an inline snippet.

Both tools share one execution path:

* the code to run is resolved first (an inline snippet is written to a temp
  file, a ``path`` is read from the project), so BOTH modes go through the
  same danger analysis - a script in the project is code too;
* the child gets a built environment (allowlist, no platform secrets, its
  own HOME) instead of a copy of ``os.environ``;
* the process tree is killed on timeout, output is read with a bound and the
  full log is written to disk;
* secrets that the child printed are redacted before the result reaches the
  model, because a tool result is sent to the provider.

Consent is read from the execution context (``sandbox_approval``), which only
the agent loop sets after the user clicked Allow. It is NOT an argument of
these tools: a value the model can emit is not a confirmation.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Optional

from core.dangerous import tool_needs_confirmation

from .. import config
from ..sandbox_approval import approvals_granted, consume_transport_approval
from ..sandbox_env import build_child_env, discard_env_home
from .deps import ToolMixin
from .exec_support import (
    confirmation_result,
    inline_command,
    result_payload,
    script_command,
    subject_for_analysis,
    subprocess_cwd,
)
from .exec_support import redact_secrets as redact_secrets


class ExecTools(ToolMixin):
    """Child processes: a test file or an inline snippet."""

    # ── shared execution path ──────────────────────────────────────────────
    def _execute(self, tool: str, *, code: Optional[str], path: Optional[str],
                 timeout_s: int) -> Dict[str, Any]:
        """Analyse and run one child process, whoever asked for it.

        The analysis covers ``code`` and ``path`` alike. Confirmation arrives
        through the execution context, never through a tool call.
        """
        if code is None and path is None:
            return {"ok": False, "error": "Provide either 'code' or 'path'."}

        try:
            subject, _label, script_path = subject_for_analysis(code, path)
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "suggestion": (
                    "Use list_files or read_file to inspect the project, "
                    "or create the script first via propose_file."
                ),
            }

        code_hash = hashlib.sha256(subject.encode("utf-8", errors="replace")).hexdigest()
        if self._safety_enabled and not approvals_granted():
            assessment = tool_needs_confirmation(tool, subject)
            if assessment.dangerous:
                # A transport cannot click: its answer is a ticket for THIS
                # tool and THESE arguments, consumed once. No ticket means
                # the run parks, which is the safe reading of "unknown".
                decision = consume_transport_approval(tool, {"code": code, "path": path})
                if decision == "allow":
                    # The ticket is consumed above, so this authorisation
                    # cannot be replayed by a second identical call.
                    return self._run_command(tool, code=code, path=path,
                                             script_path=script_path,
                                             timeout_s=timeout_s,
                                             code_hash=code_hash)
                if decision == "deny":
                    return {"ok": False, "denied_by_user": True,
                            "error": ("The user denied permission to execute "
                                      "this operation. Find another way that "
                                      "does not require this command.")}
                return confirmation_result(tool, assessment, path=script_path,
                                           code_hash=code_hash)

        return self._run_command(tool, code=code, path=path, script_path=script_path,
                                 timeout_s=timeout_s, code_hash=code_hash)

    def _run_command(self, tool: str, *, code: Optional[str], path: Optional[str],
                     script_path, timeout_s: int, code_hash: str) -> Dict[str, Any]:
        """Prepare the child process of an authorised run and execute it."""
        env = build_child_env(
            project_root=(config.PROJECT_ROOT
                          if bool(getattr(config, "WORKSPACE_SELECTED", True)) else None),
        )
        cleanup: Optional[str] = None
        try:
            if code is not None:
                cleanup, cmd = inline_command(str(code), self.deps().config.DEFAULT_ENCODING)
            else:
                target = script_path or config.resolve_in_project(path or "")
                cmd = script_command(tool, target)
            result = self._run_under_sandbox(tool, cmd, env, timeout_s, code_hash)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if cleanup:
                try:
                    os.unlink(cleanup)
                except OSError:
                    pass
            # The child's private HOME holds package caches and temporary
            # files; a new one is created for the next run, so keeping this
            # one would only accumulate one cache tree per execution.
            discard_env_home(env)

        return result_payload(result, tool, timeout_s, code_hash)

    def _run_under_sandbox(self, tool: str, cmd: list, env: dict,
                           timeout_s: int, code_hash: str) -> Dict[str, Any]:
        """Execute the prepared command through the sandbox journal.

        The sandbox decides whether the run may happen (fail-closed), picks
        the provider and records it. ``timeout_s`` caps the run below the
        profile limit: the caller reasoned about that value.
        """
        from core.sandbox import runner
        from core.sandbox.profiles import DEFAULT_PROFILE

        # The profile is a setting of the employee, not an argument of the
        # tool: a value the model can emit would let it widen its own rights.
        # The default is the isolated profile; a machine that cannot honour
        # it refuses the run instead of silently dropping the guarantee.
        profile = str(getattr(self, "_sandbox_profile", "") or DEFAULT_PROFILE)
        request = runner.build_request(
            tool=tool, argv=cmd, cwd=subprocess_cwd(), env=env,
            code_hash=code_hash, profile=profile,
            thread_id=str(getattr(config, "ACTIVE_THREAD_ID", "") or ""),
            workspace=str(config.PROJECT_ROOT),
        )
        # The wait on a child can last minutes. The paths of the request are
        # already resolved, so the process-wide workspace lock is handed back
        # for the wait: otherwise one long run would freeze tool dispatches
        # of every other dialog in the same process.
        from ..workspace_binding import without_binding_lock

        with without_binding_lock():
            result = runner.execute(request, profile=profile, timeout_s=timeout_s,
                                    subprocess_module=self.deps().subprocess)
        payload = result.to_dict()
        if payload.get("denied"):
            return {"ok": False, "denied": True,
                    "error": payload.get("error", "The run was refused.")}
        caps = dict(payload)
        caps["timed_out"] = bool(payload.get("timed_out")) or result.duration_ms > timeout_s * 1000
        return caps

    def run_test(self, code: Optional[str] = None,
                 path: Optional[str] = None) -> Dict[str, Any]:
        """Run a test in an isolated child process with a timeout.

        Two modes:
          - code: run an inline Python snippet (written to a temp file).
          - path: run pytest on a project test file/dir; the FILE ITSELF is
            read and analysed first, because pytest imports it.

        If the analysed text contains dangerous patterns and safe mode is on,
        confirmation is required: the loop stops and asks the user before
        execution.
        """
        return self._execute("run_test", code=code, path=path,
                             timeout_s=self.deps().config.MAX_TEST_TIMEOUT_SEC)

    def run_code(self, code: Optional[str] = None,
                 path: Optional[str] = None) -> Dict[str, Any]:
        """Run arbitrary Python code in an isolated child process.

        This is the universal escape hatch for operations that have no
        dedicated tool. The child process runs with a built environment (no
        platform secrets, its own HOME), a bounded output and a killable
        process tree.

        Two modes (exactly one must be provided):
          - code: run an inline Python snippet (written to a temp file).
          - path: run a Python script inside the project; the file content is
            analysed exactly as inline code.

        Returns:
            {"ok": bool, "returncode": int, "stdout": str, "stderr": str,
             "timed_out": bool, "log_path": str, "code_hash": str}
            or {"ok": False, "confirmation_required": True, ...}
            or {"ok": False, "error": str} on failure.
        """
        return self._execute("run_code", code=code, path=path,
                             timeout_s=self.deps().config.MAX_RUN_CODE_TIMEOUT_SEC)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
