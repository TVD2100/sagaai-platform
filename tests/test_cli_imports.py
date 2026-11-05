"""Tests of the console layer boundary.

The console is the fourth transport of the platform, and the promise that
makes it one is negative: it does NOT need the UI and it does NOT need the
HTTP stack. A developer machine has all of them installed, so an installed
package proves nothing - every test here runs in a subprocess where the
forbidden imports raise, exactly like ``tests/test_gateway_imports.py`` does
for the facade.

Two further promises are checked at the same level, because a violation of
either is invisible until a user hits it:

- ``sagaai`` and ``python -m sagaai`` are the same command, not two that
  happen to agree today;
- ``--help`` and ``--version`` work without importing a single command's
  transport dependency.
"""

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Every module of the console package. A new module belongs here, otherwise
#: the boundary test silently stops covering it.
CLI_MODULES = [
    "sagaai",
    "sagaai.cli",
    "sagaai.console",
    "sagaai.errors",
    "sagaai.exit_codes",
    "sagaai.commands",
    "sagaai.commands._common",
    "sagaai.commands.doctor",
    "sagaai.commands.run",
    "sagaai.commands.serve",
    "sagaai.commands.ui",
    "sagaai.commands.token",
    "sagaai.commands.update",
    "sagaai.commands.migrate",
    "sagaai.commands.backup",
    "sagaai.commands.agents",
]

#: Packages the console must never need. ``streamlit`` is the UI (the ``ui``
#: command launches it as a child process instead of importing it); the rest
#: are the HTTP transport, which only ``serve`` reaches and only lazily.
FORBIDDEN_PACKAGES = ("streamlit", "fastapi", "uvicorn", "pydantic",
                      "starlette")


def _run_blocked(module_names, blocked):
    """Import *module_names* in a subprocess where *blocked* is unimportable."""
    script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "BLOCKED = %r\n"
        "class Blocker:\n"
        "    def find_spec(self, name, *a, **k):\n"
        "        for prefix in BLOCKED:\n"
        "            if name == prefix or name.startswith(prefix + '.'):\n"
        "                raise ImportError('blocked import: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "for mod in %r:\n"
        "    __import__(mod)\n"
        "    print('OK', mod)\n"
    ) % (ROOT, tuple(blocked), list(module_names))
    return subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=180,
                          cwd=ROOT)


def _run_cli(args, timeout=120):
    """Run the console as a real process and return its completed result."""
    return subprocess.run([sys.executable, "-m", "sagaai", *args],
                          capture_output=True, text=True, timeout=timeout,
                          cwd=ROOT)


# ─── The console does not need the UI or the HTTP stack ─────────────────────


@pytest.mark.parametrize("module_name", CLI_MODULES)
def test_every_cli_module_imports_without_ui_and_transport(module_name):
    result = _run_blocked([module_name], FORBIDDEN_PACKAGES)
    assert result.returncode == 0, (
        "%s failed to import without %s\nstdout: %s\nstderr: %s"
        % (module_name, ", ".join(FORBIDDEN_PACKAGES),
           result.stdout, result.stderr))
    assert "OK " + module_name in result.stdout


def test_the_package_imports_as_a_whole_blocked():
    result = _run_blocked(CLI_MODULES, FORBIDDEN_PACKAGES)
    assert result.returncode == 0, result.stderr
    for name in CLI_MODULES:
        assert "OK " + name in result.stdout


def test_the_console_does_not_import_the_ui_package():
    """The direction is console -> core; ``ui`` is never a dependency."""
    result = _run_blocked(["sagaai.cli"], ("ui",))
    assert result.returncode == 0, result.stderr


def test_the_console_does_not_import_the_server_package():
    """``serve`` reaches the launcher lazily; the package must not import it."""
    result = _run_blocked(CLI_MODULES, ("server",))
    assert result.returncode == 0, (
        "the console imports the server package at module level\n%s"
        % result.stderr)


# ─── Two entry points, one behaviour ────────────────────────────────────────


def test_python_dash_m_and_the_script_agree_on_help():
    by_module = _run_cli(["--help"])
    script = os.path.join(ROOT, "bin", "sagaai")
    assert os.path.isfile(script), "bin/sagaai is missing"
    by_script = subprocess.run([sys.executable, script, "--help"],
                               capture_output=True, text=True, timeout=120,
                               cwd=ROOT)
    assert by_module.returncode == 0
    assert by_script.returncode == 0
    assert by_module.stdout == by_script.stdout


def test_both_entry_points_report_the_same_version():
    by_module = _run_cli(["--version"])
    script = os.path.join(ROOT, "bin", "sagaai")
    by_script = subprocess.run([sys.executable, script, "--version"],
                               capture_output=True, text=True, timeout=120,
                               cwd=ROOT)
    assert by_module.returncode == 0 and by_script.returncode == 0
    assert by_module.stdout.strip() == by_script.stdout.strip()


def test_the_script_is_executable():
    """A launcher that cannot be executed is not a command on PATH."""
    if os.name != "posix":
        pytest.skip("the executable bit is a POSIX concept")
    script = os.path.join(ROOT, "bin", "sagaai")
    assert os.access(script, os.X_OK), "bin/sagaai is not executable"


# ─── Help and version work without a transport dependency ──────────────────


def test_help_works_with_the_http_stack_blocked():
    result = _run_blocked(["sagaai.cli"], ("fastapi", "uvicorn", "pydantic"))
    assert result.returncode == 0, result.stderr


def test_every_command_declares_the_exit_codes_in_its_help():
    """A command whose --help hides the codes cannot be scripted against."""
    for command in ("doctor", "run", "serve", "ui", "token", "update",
                    "migrate", "backup", "agents"):
        result = _run_cli([command, "--help"])
        assert result.returncode == 0, (command, result.stderr)
        text = result.stdout
        assert "exit codes" in text, command
        for code_line in ("0  success", "1  the operation failed",
                          "2  the configuration", "3  another instance"):
            assert code_line in text, (command, code_line)


def test_the_top_level_help_lists_every_command():
    result = _run_cli(["--help"])
    assert result.returncode == 0
    for command in ("doctor", "run", "serve", "ui", "token", "update",
                    "migrate", "backup", "agents"):
        assert command in result.stdout, command
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
