"""Child processes: a test file or an inline snippet."""
from __future__ import annotations

import os
import sys
import tempfile
from typing import Any, Dict, Optional

from core.dangerous import tool_needs_confirmation

from .. import config
from .deps import ToolMixin


class ExecTools(ToolMixin):
    """Child processes: a test file or an inline snippet."""

    def run_test(self, code: Optional[str] = None,
                 path: Optional[str] = None,
                 confirmed_by_user: bool = False) -> Dict[str, Any]:
        """Run a test in an isolated child process with a timeout.

        Two modes:
          - code: run an inline Python snippet (written to a temp file).
          - path: run pytest on a project test file/dir.
        Captures stdout/stderr and the return code. Never runs untrusted code
        against protected files; this executes in a fresh subprocess.

        If the code contains dangerous patterns and safe mode is enabled,
        confirmation is required. The agent loop will stop and ask the user
        before execution.
        """
        # Safety gate: check for dangerous content only while safe mode is on.
        if self._safety_enabled and code is not None:
            assessment = tool_needs_confirmation("run_test", str(code))
            if assessment.dangerous and not confirmed_by_user:
                return {
                    "ok": False,
                    "confirmation_required": True,
                    "tool": "run_test",
                    "danger_assessment": assessment.to_dict(),
                    "error": (
                        "This test code requires user confirmation before execution. "
                        "The agent loop will stop and show a confirmation dialog."
                    ),
                }

        # Per-call env/cwd: read config.* here, inside the dispatch critical
        # section (workspace_binding.thread_context), so subprocesses always
        # inherit THIS thread's root. os.environ is never mutated.
        env = dict(os.environ)
        # Make the project importable as a package (sagaai.dev_agent...).
        env["PYTHONPATH"] = str(self.deps().config.PROJECT_ROOT.parent) + os.pathsep + env.get("PYTHONPATH", "")
        # Child processes must resolve runtime data (DB, history, connectors)
        # to the SAME DATA_DIR as this process, even when it was set via the
        # environment before startup and is absent from os.environ.
        try:
            import core.paths as _paths
            if _paths.DATA_DIR:
                env.setdefault("SAGAAI_DATA_DIR", str(_paths.DATA_DIR))
        except Exception:
            pass

        cleanup = None
        try:
            if code is not None:
                tf = tempfile.NamedTemporaryFile(
                    "w", suffix=".py", delete=False, encoding=self.deps().config.DEFAULT_ENCODING
                )
                tf.write(code)
                tf_path = tf.name
                tf.close()
                cleanup = tf_path
                cmd = [sys.executable, tf_path]
            elif path is not None:
                target = self.deps().config.resolve_in_project(path)
                if not target.exists():
                    return {
                        "ok": False,
                        "error": f"Test file not found: {path}",
                        "suggestion": (
                            "Use list_files or read_file to inspect the project, "
                            "or create the test file first via propose_file."
                        ),
                    }
                cmd = [sys.executable, "-m", "pytest", "-q", str(target)]
            else:
                return {"ok": False, "error": "Provide either 'code' or 'path'."}

            proc = self.deps().subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.deps().config.MAX_TEST_TIMEOUT_SEC, env=env,
                cwd=_subprocess_cwd(),
            )
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            }
        except self.deps().subprocess.TimeoutExpired:
            return {"ok": False, "error": f"Test timed out after {self.deps().config.MAX_TEST_TIMEOUT_SEC}s"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if cleanup:
                try:
                    os.unlink(cleanup)
                except Exception:
                    pass

    def run_code(self, code: Optional[str] = None,
                 path: Optional[str] = None,
                 confirmed_by_user: bool = False) -> Dict[str, Any]:
        """Run arbitrary Python code in an isolated subprocess with a 5-minute timeout.

        This is the UNIVERSAL ESCAPE HATCH for operations that have no dedicated
        tool. Use run_code when you need to perform non-standard tasks such as:
          - Install a missing package (pip install ...).
          - Write a file when propose_file fails (bypasses SafeWriter).
          - Test an external API endpoint.
          - Run shell commands, invoke git, or execute any script.
          - Perform filesystem operations not covered by the built-in tools.

        **Safety gate**: when safe mode is enabled and the code contains
        potentially dangerous patterns (destructive commands, system
        modification, network operations, etc.), execution is blocked until
        the user explicitly confirms via the UI.

        Two modes (exactly one must be provided):
          - code: run an inline Python snippet (written to a temp file).
          - path: run a Python script located inside the project.

        The process times out after 5 minutes (300 s). Captures stdout/stderr
        (last 4000 chars each) and the return code. The project root is added
        to PYTHONPATH automatically so project imports work.

        Returns:
            {"ok": bool, "returncode": int, "stdout": str, "stderr": str}
            or {"ok": False, "confirmation_required": True, ...}
            or {"ok": False, "error": str} on failure.
        """
        # Safety gate: check for dangerous content only while safe mode is on.
        if self._safety_enabled and code is not None:
            assessment = tool_needs_confirmation("run_code", str(code))
            if assessment.dangerous and not confirmed_by_user:
                return {
                    "ok": False,
                    "confirmation_required": True,
                    "tool": "run_code",
                    "danger_assessment": assessment.to_dict(),
                    "error": (
                        "This code requires user confirmation before execution. "
                        "The agent loop will stop and show a confirmation dialog."
                    ),
                }

        env = dict(os.environ)
        # Make the project importable as a package (sagaai.dev_agent...).
        env["PYTHONPATH"] = str(self.deps().config.PROJECT_ROOT.parent) + os.pathsep + env.get("PYTHONPATH", "")
        # Same DATA_DIR propagation as run_test: child code must see this
        # process's runtime data dir.
        try:
            import core.paths as _paths
            if _paths.DATA_DIR:
                env.setdefault("SAGAAI_DATA_DIR", str(_paths.DATA_DIR))
        except Exception:
            pass

        cleanup = None
        try:
            if code is not None:
                tf = tempfile.NamedTemporaryFile(
                    "w", suffix=".py", delete=False, encoding=self.deps().config.DEFAULT_ENCODING
                )
                tf.write(code)
                tf_path = tf.name
                tf.close()
                cleanup = tf_path
                cmd = [sys.executable, tf_path]
            elif path is not None:
                target = self.deps().config.resolve_in_project(path)
                if not target.exists():
                    return {
                        "ok": False,
                        "error": f"Script file not found: {path}",
                        "suggestion": (
                            "Use list_files or read_file to inspect the project, "
                            "or create the file first via propose_file."
                        ),
                    }
                cmd = [sys.executable, str(target)]
            else:
                return {"ok": False, "error": "Provide either 'code' or 'path'."}

            proc = self.deps().subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.deps().config.MAX_RUN_CODE_TIMEOUT_SEC, env=env,
                cwd=_subprocess_cwd(),
            )
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            }
        except self.deps().subprocess.TimeoutExpired:
            return {"ok": False, "error": f"run_code timed out after {self.deps().config.MAX_RUN_CODE_TIMEOUT_SEC}s"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if cleanup:
                try:
                    os.unlink(cleanup)
                except Exception:
                    pass


def _subprocess_cwd() -> str:
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


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
