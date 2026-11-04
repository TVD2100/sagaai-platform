"""Tests of the gateway layer boundary.

The facade exists so a non-UI transport can use the core. That promise is
only worth something if it is enforced: the package must import in a
process without streamlit, fastapi or pydantic, and it must not reach for
the UI or for the transport modules even transitively.

Each test runs in a subprocess with an import blocker rather than relying
on the environment: a developer machine may have all three packages
installed, and an installed package never proves the import graph.
"""

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Every module of the facade. A new module belongs in this list, otherwise
#: the boundary test silently stops covering it.
GATEWAY_MODULES = [
    "core.gateway",
    "core.gateway.dto",
    "core.gateway.errors",
    "core.gateway.health",
    "core.gateway.threads",
    "core.gateway.entities",
    "core.gateway.runs",
    "core.gateway.dialog",
    "core.orchestration.dialog_runner",
    "core.api_token_store",
]

#: Packages the facade must never need. ``streamlit`` is the UI, ``fastapi``
#: and ``pydantic`` are the transport, and the facade sits below both.
FORBIDDEN_PACKAGES = ("streamlit", "fastapi", "pydantic", "starlette", "uvicorn")


def _run_blocked(module_names, blocked):
    """Import *module_names* in a subprocess where *blocked* is unimportable."""
    blocked_tuple = tuple(blocked)
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
    ) % (ROOT, blocked_tuple, list(module_names))
    return subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=180)


@pytest.mark.parametrize("module_name", GATEWAY_MODULES)
def test_gateway_imports_without_ui_and_transport(module_name):
    """Each facade module imports with streamlit, fastapi and pydantic blocked."""
    result = _run_blocked([module_name], FORBIDDEN_PACKAGES)
    assert result.returncode == 0, (
        "%s failed to import without %s\nstdout: %s\nstderr: %s"
        % (module_name, ", ".join(FORBIDDEN_PACKAGES),
           result.stdout, result.stderr)
    )
    assert "OK " + module_name in result.stdout


def test_gateway_package_imports_as_a_whole():
    """The package surface imports in one go, blocked exactly like above."""
    result = _run_blocked(GATEWAY_MODULES, FORBIDDEN_PACKAGES)
    assert result.returncode == 0, result.stderr
    for module_name in GATEWAY_MODULES:
        assert "OK " + module_name in result.stdout


def test_gateway_does_not_import_the_ui_package():
    """The facade never imports ``ui`` - the direction is ui -> gateway."""
    result = _run_blocked(["core.gateway"], ("ui",))
    assert result.returncode == 0, (
        "core.gateway pulled in the ui package\nstdout: %s\nstderr: %s"
        % (result.stdout, result.stderr)
    )


def test_gateway_does_not_import_streamlit_auth():
    """The auth module of the UI is never a transitive dependency.

    ``core.auth`` imports streamlit at module level, so an accidental import
    of it from the facade would drag the whole UI into the HTTP process. The
    blocker is a module path here, not a package name.
    """
    script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "class Blocker:\n"
        "    def find_spec(self, name, *a, **k):\n"
        "        if name == 'core.auth':\n"
        "            raise ImportError('core.auth is forbidden in the gateway')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "import core.gateway\n"
        "import core.gateway.dialog\n"
        "print('OK')\n"
    ) % ROOT
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, (
        "the gateway imports core.auth (which imports streamlit)\n"
        "stdout: %s\nstderr: %s" % (result.stdout, result.stderr)
    )


def test_dialog_runner_imports_without_streamlit():
    """The step builder stays usable without the UI as well."""
    result = _run_blocked(["core.orchestration.dialog_runner"],
                          FORBIDDEN_PACKAGES)
    assert result.returncode == 0, result.stderr
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
