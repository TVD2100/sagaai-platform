"""Environment for child processes spawned by run_code / run_test.

The child process must not inherit the platform's process environment: that
environment carries provider API keys, connector tokens and the runtime data
directory, and a child started by the agent can print or forward any of it.
The builder therefore copies a fixed allowlist and adds explicit paths.

This module is dependency-free on purpose (stdlib only) so it can be imported
from anywhere in the tool layer without an import cycle.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Dict, Optional

#: Variables copied verbatim from the parent when present. They describe the
#: machine (locale, search path, system root), never a secret.
ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TERM",
)

#: Prefixes that are NEVER forwarded, even when explicitly requested. The
#: check is a safety net for callers that pass an extra mapping.
_BLOCKED_PREFIXES = ("SAGAAI_", "AWS_", "GITHUB_", "OPENAI_", "ANTHROPIC_",
                     "YANDEX_", "DEEPSEEK_", "GIGACHAT_")


class _BlockedEnv(dict):
    """A dict that refuses to store the platform's own variables.

    ``env`` is handed to ``subprocess`` and also read back by callers and
    tests; blocking here is the last line of defence if a caller ignores the
    allowlist and passes an extra mapping.
    """

    @staticmethod
    def _forbidden(key: str) -> bool:
        upper = str(key).upper()
        if upper in ("SSH_AUTH_SOCK", "PYTHONPATH"):
            return False if upper == "PYTHONPATH" else True
        return any(upper.startswith(p) for p in _BLOCKED_PREFIXES)

    def __setitem__(self, key, value):  # noqa: D105 - dict protocol
        if self._forbidden(key):
            return
        super().__setitem__(key, value)

    def update(self, *args, **kwargs):  # noqa: D102 - dict protocol
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):  # noqa: D102 - dict protocol
        if self._forbidden(key):
            return default
        return super().setdefault(key, default)


def build_child_env(*, project_root: Optional[Path],
                    include_project: bool = True,
                    sandbox_home: Optional[Path] = None,
                    extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build the environment of a child process.

    Args:
        project_root: active workspace root, used for ``PYTHONPATH`` and as
            the base for the temporary home. ``None`` means the caller has no
            workspace (empty state); the child then gets a neutral home only.
        include_project: put *project_root* itself on ``PYTHONPATH``. Only the
            project is added, never its parent directory: a sibling project
            must not become importable.
        sandbox_home: explicit home directory; a temp one is created when
            omitted.
        extra: additional variables, filtered through the same rules.

    Returns:
        A new dict; the parent ``os.environ`` is never mutated.
    """
    env: Dict[str, str] = _BlockedEnv()
    for name in ENV_ALLOWLIST:
        value = os.environ.get(name)
        if value:
            env[name] = value

    home = Path(sandbox_home) if sandbox_home else Path(tempfile.mkdtemp(prefix="sagaai-run-"))
    tmp_dir = home / "tmp"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["TMPDIR"] = str(tmp_dir)
    env["TEMP"] = str(tmp_dir)
    env["TMP"] = str(tmp_dir)
    env["PIP_CACHE_DIR"] = str(home / "pip-cache")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONUTF8"] = "1"

    if include_project and project_root is not None:
        env["PYTHONPATH"] = str(Path(project_root))
    else:
        env.pop("PYTHONPATH", None)

    if extra:
        env.update(extra)
    return dict(env)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
