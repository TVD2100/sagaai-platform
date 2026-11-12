"""Environment for child processes spawned by run_code / run_test.

The child process must not inherit the platform's process environment: that
environment carries provider API keys, connector tokens and the runtime data
directory, and a child started by the agent can print or forward any of it.
The builder therefore copies a fixed allowlist and adds explicit paths.

An employee the owner granted full access to is the one exception: its child
receives the platform environment unchanged, because a task that manages the
machine needs the credentials the platform itself uses. The grant is a single
switch of the employee settings, off by default; the output redaction of the
execution tools still applies, so a printed value does not reach the provider.

This module is dependency-free on purpose (stdlib only) so it can be imported
from anywhere in the tool layer without an import cycle.
"""

from __future__ import annotations

import os
import shutil
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

#: Environment variable a sandbox provider stamps into a child with the
#: grants of the policy it applied (see ``core.sandbox.inheritance``). It is
#: forwarded deliberately: a nested run must be able to tell that it already
#: executes inside a sandbox, and a lost marker would make it try to nest.
SANDBOX_GRANTS_VAR = "SAGAAI_SANDBOX_GRANTS"

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
        if upper == "SSH_AUTH_SOCK":
            return True
        if upper == SANDBOX_GRANTS_VAR:
            # A capability description, not a secret: the child needs it to
            # avoid entering a second, forbidden sandbox.
            return False
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


#: Prefix of the temporary HOME this module creates for a child process.
#: The name is for the owner's eyes only - cleanup relies on the process
#: registry below, never on the shape of the path.
AUTO_HOME_PREFIX = "sagaai-run-"

#: Homes this module created in the current process. Cleanup uses this set
#: instead of the shape of the path: a real home directory may legitimately
#: be called ``sagaai-run-...``, and removing it would delete the owner's
#: files. The set lives in memory because the environment handed to a child
#: must carry no platform variable at all.
_CREATED_HOMES: set = set()


def discard_env_home(env: Optional[Dict[str, str]]) -> bool:
    """Remove the temporary HOME that ``build_child_env`` created for *env*.

    A run needs its own HOME and TMPDIR, but nothing needs them afterwards:
    left behind, they accumulate one directory of caches per execution and
    quietly fill the disk. Removal is best effort - a leftover directory
    must never turn a finished run into an error.

    Only a directory this module created is removed. The set of created
    homes is the proof: the name prefix alone is not, because a real home
    directory may legitimately carry it. A caller that supplied
    ``sandbox_home`` explicitly owns it and is left to clean it up itself.

    Returns:
        True when a directory was removed, False when there was nothing of
        ours to remove.
    """
    home = str((env or {}).get("HOME") or "")
    if not home or home not in _CREATED_HOMES:
        return False
    path = Path(home)
    if not path.name.startswith(AUTO_HOME_PREFIX):
        return False
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:  # pragma: no cover - defensive
        return False
    return not path.exists()


def build_child_env(*, project_root: Optional[Path],
                    include_project: bool = True,
                    sandbox_home: Optional[Path] = None,
                    isolate_env: bool = True,
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
            omitted and the child is isolated.
        isolate_env: ``True`` builds the environment from the allowlist;
            ``False`` is the full-access grant - the platform environment is
            copied unchanged, with the project on ``PYTHONPATH`` ahead of any
            inherited entry. The default is the isolated form: a caller that
            says nothing grants nothing.
        extra: additional variables, filtered through the same rules.

    Returns:
        A new dict; the parent ``os.environ`` is never mutated.
    """
    isolated = bool(isolate_env)
    env: Dict[str, str]
    if isolated:
        env = _BlockedEnv()
        for name in ENV_ALLOWLIST:
            value = os.environ.get(name)
            if value:
                env[name] = value
    else:
        # Full access: the platform environment is exactly what a task that
        # manages the machine needs, so it is copied unchanged. The hard
        # block list of the isolated path exists to keep secrets from a
        # child that must not have them and does not apply here.
        env = dict(os.environ)

    home: Optional[Path] = Path(sandbox_home) if sandbox_home else None
    if home is None and isolated:
        home = Path(tempfile.mkdtemp(prefix=AUTO_HOME_PREFIX))
        _CREATED_HOMES.add(str(home))
    if home is not None:
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        if isolated:
            tmp_dir = home / "tmp"
            try:
                tmp_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            env["TMPDIR"] = str(tmp_dir)
            env["TEMP"] = str(tmp_dir)
            env["TMP"] = str(tmp_dir)
            env["PIP_CACHE_DIR"] = str(home / "pip-cache")

    if isolated:
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONUTF8"] = "1"
    else:
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")

    if include_project and project_root is not None:
        root = str(Path(project_root))
        if isolated:
            env["PYTHONPATH"] = root
        else:
            inherited_path = str(env.get("PYTHONPATH") or "")
            rest = [item for item in inherited_path.split(os.pathsep)
                    if item and item != root]
            env["PYTHONPATH"] = os.pathsep.join([root] + rest)
    elif isolated:
        env.pop("PYTHONPATH", None)

    # An enclosing sandbox is inherited, not re-created: the child must be
    # able to see that it already runs confined.
    inherited = os.environ.get(SANDBOX_GRANTS_VAR, "").strip()
    if inherited:
        env[SANDBOX_GRANTS_VAR] = inherited

    if extra:
        env.update(extra)
    return dict(env)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
