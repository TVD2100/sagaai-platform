"""Security tests for the execution gate (phase 0).

Each test pins one concrete bypass that existed before the gate was fixed:

* a model-supplied ``confirmed_by_user`` removed the confirmation;
* ``path`` mode was never analysed;
* only the first 2000 characters of the code were analysed;
* a whitelisted binary vouched for every subprocess call in the snippet;
* interpreters and package managers were themselves whitelisted;
* the child inherited the whole parent environment.
"""
import json
import os
import subprocess
from unittest import mock

import pytest

from core.dangerous import (
    ANALYSIS_LIMIT_CHARS,
    assess_code,
    tool_needs_confirmation,
)
from dev_agent.loop.json_repair import _normalize_call
from dev_agent.sandbox_env import build_child_env
from dev_agent.tool_executor import ToolExecutor
from dev_agent.tools.exec import ExecTools, redact_secrets

# ── the model can no longer confirm its own operation ───────────────────────


def test_the_flag_reaches_the_dispatcher_instead_of_vanishing():
    """The parser keeps the flag so the model can be told why it is refused.

    A silent drop made the refused call look successful: the model saw a
    normal result and never learned that it cannot confirm its own
    operation. The flag therefore travels one step further and is rejected
    by the dispatcher with a structured error.
    """
    call = _normalize_call({
        "tool": "run_code",
        "args": {"code": "print(1)", "confirmed_by_user": True},
    })
    assert call is not None
    assert call["args"]["confirmed_by_user"] is True
    assert call["args"]["code"] == "print(1)"


def test_other_arguments_are_preserved():
    """Normalisation must not strip legitimate arguments."""
    call = _normalize_call({
        "tool": "run_code",
        "args": {"code": "x = 1", "path": "", "confirmed_by_user": True},
    })
    assert call["args"]["code"] == "x = 1"
    assert call["args"]["path"] == ""


def test_executor_rejects_the_flag_as_an_unknown_argument():
    """A direct call with the flag is refused by the argument validator."""
    result = ToolExecutor().dispatch("run_code", {
        "code": "print(1)", "confirmed_by_user": True,
    })
    assert result["ok"] is False
    assert "confirmed_by_user" in result.get("unknown_args", [])


def test_run_test_also_rejects_the_flag():
    """Both execution tools refuse the flag, not just run_code."""
    result = ToolExecutor().dispatch("run_test", {
        "path": "tests/test_x.py", "confirmed_by_user": "true",
    })
    assert result["ok"] is False
    assert "confirmed_by_user" in result.get("unknown_args", [])


def test_approval_context_is_not_granted_by_default():
    """Outside the loop's context the gate stays on."""
    from dev_agent.sandbox_approval import approval_context, approvals_granted

    assert approvals_granted() is False
    with approval_context():
        assert approvals_granted() is True
    assert approvals_granted() is False


# ── analysis covers the whole text ──────────────────────────────────────────


def test_payload_after_a_long_prologue_is_found():
    """A 400-line padding prologue cannot hide the payload that follows."""
    padding = "# filler line\n" * 400
    code = padding + "import shutil\nshutil.rmtree('/important')\n"
    assert len(code) > 2000
    assessment = tool_needs_confirmation("run_code", code)
    assert assessment.dangerous is True
    assert any("rmtree" in reason for reason in assessment.reasons)


def test_analysis_limit_is_far_above_a_realistic_snippet():
    """The analysis bound exists only as a guard against pathological input."""
    assert ANALYSIS_LIMIT_CHARS >= 100_000


def test_display_snippet_is_marked_when_shortened():
    """The user is told when the preview is not the whole code."""
    code = "x = 1\n" * 10_000
    assessment = assess_code(code, tool="run_code")
    assert len(assessment.code_snippet) < len(code)
    assert "анализ выполнен по всему тексту" in assessment.code_snippet


# ── one whitelisted call cannot vouch for a dangerous one ───────────────────


def test_safe_call_does_not_suppress_a_dangerous_call():
    """echo + rm in one snippet is dangerous, whatever the first call is."""
    code = (
        "import subprocess\n"
        "subprocess.run(['echo', 'ok'])\n"
        "subprocess.run(['rm', '-rf', '/tmp/x'])\n"
    )
    assessment = tool_needs_confirmation("run_code", code)
    assert assessment.dangerous is True


def test_only_safe_calls_stay_allowed():
    """Two genuinely harmless calls remain non-dangerous."""
    code = (
        "import subprocess\n"
        "subprocess.run(['echo', 'one'])\n"
        "subprocess.run(['ls', '-la'])\n"
    )
    assessment = tool_needs_confirmation("run_code", code)
    assert assessment.dangerous is False


def test_open_safe_name_does_not_excuse_a_second_write():
    """A harmless open() no longer excuses an open() of a system path."""
    code = "open('test.txt', 'w')\nopen('/etc/passwd', 'w')\n"
    assessment = tool_needs_confirmation("run_code", code)
    assert assessment.dangerous is True


# ── interpreters and package managers are not safe binaries ─────────────────


@pytest.mark.parametrize("command", [
    "python", "python3", "node", "npm", "npx", "pytest",
    "pip", "pip3", "git", "make", "cargo", "go", "php", "sqlite3",
])
def test_build_and_test_commands_run_without_a_confirmation(command):
    """The ordinary work of a project is allowed in list form.

    A test runner, a version-control command or a language runtime is what
    an agent is asked to use. Blocking them meant that a delegated child -
    which has nobody to confirm anything - was refused the task itself.
    """
    code = f"import subprocess\nsubprocess.run(['{command}', '--version'])\n"
    assessment = tool_needs_confirmation("run_code", code)
    assert assessment.dangerous is False


@pytest.mark.parametrize("command", [
    "rm", "shred", "dd", "mkfs", "sudo", "su", "curl", "wget",
    "ssh", "scp", "rsync", "kill", "pkill", "shutdown", "reboot",
    "apt-get", "brew", "systemctl", "iptables",
])
def test_destructive_commands_still_ask_even_in_list_form(command):
    """The deny list is what the list form cannot get around.

    These binaries destroy data, control the machine or reach another one;
    a list-form call does not make them safe to run unattended.
    """
    code = f"import subprocess\nsubprocess.run(['{command}', 'x'])\n"
    assessment = tool_needs_confirmation("run_code", code)
    assert assessment.dangerous is True


@pytest.mark.parametrize("command", ["env", "printenv"])
def test_environment_dump_is_still_dangerous(command):
    """Reading the environment can print a secret, so it keeps asking."""
    code = f"import subprocess\nsubprocess.run(['{command}'])\n"
    assert tool_needs_confirmation("run_code", code).dangerous is True


@pytest.mark.parametrize("command", ["docker", "kubectl", "terraform", "poetry", "conda"])
def test_deploy_and_environment_managers_still_ask(command):
    """A tool that owns machine-level state is outside an ordinary cycle."""
    code = f"import subprocess\nsubprocess.run(['{command}', '--version'])\n"
    assert tool_needs_confirmation("run_code", code).dangerous is True


def test_a_string_command_with_shell_is_still_dangerous():
    """Only the LIST form rules out shell injection; the string form keeps it."""
    code = "import subprocess\nsubprocess.run('ls -la', shell=True)\n"
    assert tool_needs_confirmation("run_code", code).dangerous is True


def test_a_dangerous_command_is_not_excused_by_an_allowed_one():
    """A deny-listed binary is refused even next to an allowed one."""
    code = (
        "import subprocess\n"
        "subprocess.run(['pytest', '-q'])\n"
        "subprocess.run(['rm', '-rf', '/tmp/x'])\n"
    )
    assert tool_needs_confirmation("run_code", code).dangerous is True


def test_environment_dump_is_dangerous():
    """Printing the environment exposes secrets, so it needs a confirmation."""
    code = "import subprocess\nsubprocess.run(['env'])\n"
    assert tool_needs_confirmation("run_code", code).dangerous is True


def test_a_comment_about_a_forbidden_command_is_not_a_command():
    """A warning that names a forbidden command is documentation.

    Matching the shell rules against comments refused a snippet for the text
    of its own warning, which pushed the model into rewriting working code.
    """
    code = "# never run sudo here: the child has no rights for it\nprint(1)\n"
    assert tool_needs_confirmation("run_code", code).dangerous is False


def test_a_string_replace_is_not_a_file_write():
    """``str.replace`` shares its name with the pathlib method.

    Treating every ``.replace(`` as a write blocked ordinary text work and
    had nothing to do with the file the snippet would touch.
    """
    code = "name = 'report.md'.replace('md', 'txt')\nprint(name)\n"
    assert tool_needs_confirmation("run_code", code).dangerous is False


# ── APIs that the old rules did not see ─────────────────────────────────────


@pytest.mark.parametrize("code", [
    "from subprocess import run\nrun(['rm', '-rf', '/x'])\n",
    "import os\nos.popen('rm -rf /x')\n",
    "import asyncio\nasyncio.create_subprocess_exec('rm', '-rf', '/x')\n",
    "import requests\nrequests.get('https://example.com')\n",
    "import urllib.request\nurllib.request.urlopen('https://example.com')\n",
    "from pathlib import Path\nPath('/etc/passwd').write_text('x')\n",
    "import ctypes\nctypes.CDLL('libc.so.6')\n",
    "import importlib\nimportlib.import_module('os')\n",
    "__import__('os').system('id')\n",
])
def test_alternative_apis_are_detected(code):
    """Process, network, native-code and pathlib writes are all covered."""
    assert tool_needs_confirmation("run_code", code).dangerous is True


# ── path mode goes through the same gate ────────────────────────────────────


def test_path_mode_is_analysed(tmp_path, monkeypatch):
    """A dangerous script referenced by path needs confirmation too."""
    script = tmp_path / "evil.py"
    script.write_text("import shutil\nshutil.rmtree('/x')\n", encoding="utf-8")

    te = ExecTools()
    te._safety_enabled = True
    monkeypatch.setattr(
        "dev_agent.tools.exec.config.resolve_in_project", lambda p: script,
    )
    result = te.run_code(path="evil.py")
    assert result["confirmation_required"] is True
    assert result["danger_assessment"]["dangerous"] is True


def test_path_mode_safe_script_runs(tmp_path, monkeypatch):
    """A harmless script runs and reports the usual result keys."""
    script = tmp_path / "ok.py"
    script.write_text("print('hello from path mode')\n", encoding="utf-8")

    te = ExecTools()
    te._safety_enabled = True
    monkeypatch.setattr(
        "dev_agent.tools.exec.config.resolve_in_project", lambda p: script,
    )
    result = te.run_code(path="ok.py")
    assert result.get("confirmation_required") is not True
    assert result["ok"] is True
    assert "hello from path mode" in result["stdout"]


def test_binary_script_is_refused(tmp_path, monkeypatch):
    """A binary file is refused instead of being decoded and run."""
    script = tmp_path / "blob.py"
    script.write_bytes(b"\x00\x01\x02binary")

    te = ExecTools()
    monkeypatch.setattr(
        "dev_agent.tools.exec.config.resolve_in_project", lambda p: script,
    )
    result = te.run_code(path="blob.py")
    assert result["ok"] is False
    assert "binary" in result["error"].lower()


# ── secrets printed by the child are redacted ───────────────────────────────


def test_printed_secret_values_are_redacted():
    """A credential-looking value never reaches the model context."""
    text = "API_KEY=sk-abcdefghijklmnopqrstuvwxyz\ntoken: ghp_abcdefghijklmnopqrstuvwxyz\n"
    redacted = redact_secrets(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "[REDACTED]" in redacted


def test_redaction_keeps_ordinary_text():
    """Normal output is untouched."""
    text = "processed 42 rows in 1.2s"
    assert redact_secrets(text) == text


def test_redaction_covers_environment_names():
    """A full-access child can dump the environment; names are covered too.

    The field pattern looks for the credential word right before the
    separator, so an assignment whose word sits at the END of the name used
    to pass through untouched - exactly the shape of the platform keys.
    """
    text = ("SAGAAI_YANDEXAI_KEY=secret-value\n"
            "SAGAAI_DEEPSEEK_KEY: another-secret\n"
            "OPENAI_API_TOKEN=tok-1234567890\n")
    redacted = redact_secrets(text)
    assert "secret-value" not in redacted
    assert "another-secret" not in redacted
    assert "tok-1234567890" not in redacted
    assert "SAGAAI_YANDEXAI_KEY=[REDACTED]" in redacted


def test_redaction_keeps_paths_with_credential_words():
    """A path that merely mentions a key is not a credential."""
    text = "reading /opt/keys/public.pem\n"
    assert redact_secrets(text) == text


# ── the child environment is built, not inherited ───────────────────────────


def test_child_env_has_no_platform_variables():
    """None of the platform's own variables survive into the child."""
    env = build_child_env(project_root=None)
    leaked = [key for key in env if key.upper().startswith("SAGAAI_")]
    assert leaked == []
    assert "SSH_AUTH_SOCK" not in env


# ── the process tree is killed and output is bounded ────────────────────────


def test_timeout_kills_the_process_group(tmp_path):
    """A timed-out run is reported with its partial output, not silence."""
    from dev_agent.sandbox_proc import run_child

    code = "import time\nprint('started', flush=True)\ntime.sleep(30)\n"
    script = tmp_path / "slow.py"
    script.write_text(code, encoding="utf-8")

    result = run_child(
        [os.sys.executable, str(script)],
        env=build_child_env(project_root=tmp_path),
        cwd=str(tmp_path),
        timeout_s=1,
        log_path=tmp_path / "run.log",
    )
    assert result["timed_out"] is True
    assert result["ok"] is False
    assert "started" in result["stdout"]
    assert (tmp_path / "run.log").exists()


def test_output_is_bounded_in_memory(tmp_path):
    """A printing loop cannot exhaust the platform's memory."""
    from dev_agent.sandbox_proc import run_child

    code = "for _ in range(20000):\n    print('A' * 100)\n"
    script = tmp_path / "noisy.py"
    script.write_text(code, encoding="utf-8")

    result = run_child(
        [os.sys.executable, str(script)],
        env=build_child_env(project_root=tmp_path),
        cwd=str(tmp_path),
        timeout_s=60,
        output_limit=10_000,
        log_path=tmp_path / "noisy.log",
    )
    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["stdout"]) <= 20_000
    assert (tmp_path / "noisy.log").exists()


def test_stdin_is_closed(tmp_path):
    """input() gets EOF instead of hanging until the timeout."""
    from dev_agent.sandbox_proc import run_child

    code = "try:\n    value = input()\nexcept EOFError:\n    print('no-stdin')\n"
    script = tmp_path / "reads.py"
    script.write_text(code, encoding="utf-8")

    result = run_child(
        [os.sys.executable, str(script)],
        env=build_child_env(project_root=tmp_path),
        cwd=str(tmp_path),
        timeout_s=20,
        log_path=tmp_path / "reads.log",
    )
    assert result["ok"] is True
    assert "no-stdin" in result["stdout"]


# ── the batch keeps the results it produced ────────────────────────────────


def test_parked_batch_reports_suspended_calls():
    """A parked run tells the model which calls were not executed."""
    from dev_agent.loop.state import AgentLoopState
    from dev_agent.loop.steps.executing import _Batch, _park_for_confirmation

    calls = [
        {"tool": "read_file", "args": {"path": "a"}},
        {"tool": "run_code", "args": {"code": "x"}},
        {"tool": "read_file", "args": {"path": "b"}},
    ]
    state = AgentLoopState(task="t", parsed_calls=calls)
    runtime = mock.Mock()
    runtime.emit = mock.Mock()
    batch = _Batch(state, runtime)
    batch.append({"ok": True, "path": "a", "content": "first result"})
    _park_for_confirmation(batch, "run_code", calls[1], {
        "ok": False,
        "confirmation_required": True,
        "danger_assessment": {"reasons": ["r"], "code_snippet": "x"},
    })
    assert "first result" in state.user_message
    assert "suspended" in state.user_message
    assert state.pending_confirmation_tool == "run_code"
