# -*- coding: utf-8 -*-
"""T1 provider on Linux: bubblewrap (``bwrap``).

``bwrap`` builds a mount namespace for the child: the root filesystem is
mounted read-only, a private temporary directory is provided, the child's own
HOME and TMPDIR are mounted writable on top of it, the workspace is
bind-mounted read-write only when the profile is writable, and
``--unshare-net`` removes the network namespace entirely when the profile
does not grant it.

The temporary tree is private unless the profile asks for the shared one:
the files of other processes on the machine are not part of what a run was
given. Everything the run itself needs there - its HOME, its own temporary
directory, the snippet of an inline call - is mounted back by path.

A read-only profile keeps the workspace mounted read-only instead of being
omitted: the child must still be able to read the project it is inspecting.

The provider is offered only when ``bwrap`` exists AND a probe run inside it
succeeds - an unprivileged container host often has the binary but forbids
the namespace, and a refusal at run time would look like a broken script
rather than a missing guarantee.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from core.sandbox.contracts import ResourceLimits, SandboxProfile, SandboxRequest, SandboxResult
from core.sandbox.policy import SandboxProvider
from core.sandbox.providers._spawn import run_wrapped

#: Name reported in a result and stored in the journal.
PROVIDER_NAME = "bwrap"


def _resolve_bwrap() -> Optional[str]:
    """Return the bwrap executable, or None when it is not installed."""
    return shutil.which("bwrap")


def _shared_tmp_paths() -> List[str]:
    """Return the shared temporary trees that exist on this machine."""
    return [path for path in ("/tmp", "/var/tmp") if os.path.isdir(path)]


def _env_paths(env: Optional[Dict[str, str]]) -> List[str]:
    """Return the child's own HOME and temporary directories.

    These are the paths the interpreter writes its caches to before the
    generated program runs, so they are granted in every profile - the same
    grant the macOS provider makes through ``_writable_paths``.
    """
    paths: List[str] = []
    for key in ("HOME", "TMPDIR", "TMP", "TEMP"):
        value = str((env or {}).get(key) or "")
        if value and os.path.isdir(value) and value not in paths:
            paths.append(value)
    return paths


def _bind(argv: List[str], path: str, writable: bool) -> None:
    """Append a bind mount of *path* onto itself.

    A missing path is skipped: a bind mount of something that does not
    exist makes ``bwrap`` fail, and a directory nobody created needs no
    rule.
    """
    if not path or not os.path.exists(path):
        return
    argv += ["--bind" if writable else "--ro-bind", path, path]


def build_argv(bwrap: str, profile: SandboxProfile, workspace: str,
               env: Optional[Dict[str, str]] = None,
               read_paths: Optional[List[str]] = None) -> List[str]:
    """Return the bwrap arguments that implement *profile*.

    Args:
        bwrap: path of the executable.
        profile: the profile being honoured.
        workspace: project root mounted into the namespace.
        env: environment of the child; its ``HOME`` and temporary
            directories are mounted writable.
        read_paths: files outside the workspace the run must be able to
            open, mounted read-only.
    """
    argv = [bwrap, "--die-with-parent", "--new-session",
            "--ro-bind", "/", "/",
            "--dev", "/dev", "--proc", "/proc",
            # A private temporary tree: the child gets a writable /tmp of its
            # own, and the files of other processes on this machine stay
            # out of its reach. What the run legitimately needs from the
            # real tree is mounted back below, path by path.
            "--tmpfs", "/tmp"]
    if not profile.network:
        argv.append("--unshare-net")
    # Staging a file at a hardcoded /tmp path is what ``shared_tmp`` grants:
    # such a backend ignores TMPDIR, so the private tree has to be replaced
    # by the real one for a profile that asked for it.
    if profile.shared_tmp and profile.writable:
        for path in _shared_tmp_paths():
            _bind(argv, path, True)
    if workspace:
        argv += ["--bind" if profile.writable else "--ro-bind", workspace, workspace]
        argv += ["--chdir", workspace]
    # The interpreter writes its caches into HOME and its temporary files
    # into TMPDIR, so both are mounted writable - inside the private tree
    # when they live there, which recreates just those directories there.
    for path in _env_paths(env):
        _bind(argv, path, True)
    # A script the run must open but does not own - the ``path`` mode of a
    # tool - is mounted read-only: running it is not a reason to let the
    # child write into its directory.
    for path in (read_paths or []):
        _bind(argv, str(path), False)
    # Mounts are applied in order, so a later mask hides what the earlier
    # root bind made visible: the platform data directory and the user's key
    # stores are the files a generated program has no reason to see.
    from core.sandbox.providers.seatbelt import sensitive_reads

    for blocked in sensitive_reads(workspace):
        argv += ["--tmpfs", blocked]
    if workspace and profile.writable:
        # git EXECUTES hooks and can be told to run a program by its config,
        # so those entries stay read-only; the index, objects and refs stay
        # writable because staging and committing are ordinary project work.
        # Mounts apply in order, so a later read-only bind wins over the
        # writable workspace bind above.
        from core.sandbox.providers.seatbelt import protected_git_paths
        for blocked in protected_git_paths(workspace):
            argv += ["--ro-bind", blocked, blocked]
    argv.append("--")
    return argv


class BwrapProvider(SandboxProvider):
    """Run a request inside a bubblewrap mount and network namespace."""

    name = PROVIDER_NAME
    isolation = "os"
    #: The namespace mounts the root read-only, re-binds only the workspace
    #: and removes the network namespace when the profile does not allow it.
    capabilities = ("network-off", "write-confined")

    def __init__(self, subprocess_module: Any = None) -> None:
        """Store the spawn module; ``None`` means the real ``subprocess``."""
        self._subprocess = subprocess_module
        self._probe: Optional[bool] = None

    def available(self) -> bool:
        """True when bwrap exists and can start a trivial child."""
        if self._probe is not None:
            return self._probe
        self._probe = False
        executable = _resolve_bwrap()
        if not executable:
            return False
        try:
            completed = subprocess.run(
                [executable, "--ro-bind", "/", "/", "--dev", "/dev",
                 "--proc", "/proc", "--unshare-net", "--", "/bin/true"],
                capture_output=True, timeout=15,
            )
            self._probe = completed.returncode == 0
        except Exception:
            self._probe = False
        return self._probe

    def run(self, request: SandboxRequest, profile: SandboxProfile,
            limits: ResourceLimits) -> SandboxResult:
        """Execute *request* inside a bubblewrap namespace.

        A run that already executes inside a namespace is not wrapped again:
        the mounts of the enclosing namespace are the ones in force, and a
        second ``bwrap`` would only add a layer that cannot widen them. The
        grants are compared first, so a run that needs more than the
        enclosing policy gives is refused with an explanation.
        """
        from core.sandbox import inheritance

        enclosing = inheritance.enclosing_sandbox(request.env)
        if enclosing is not None:
            verdict = inheritance.compare(enclosing, profile)
            if not verdict.ok:
                return SandboxResult(ok=False, provider=self.name,
                                     isolation=enclosing.isolation,
                                     denied=True, error=verdict.error)
            return run_wrapped(self.name, enclosing.isolation, [], request,
                               limits, subprocess_module=self._subprocess)
        executable = _resolve_bwrap()
        if not executable:
            return SandboxResult(ok=False, provider=self.name, isolation=self.isolation,
                                 error="bwrap is not installed on this machine.")
        prefix = build_argv(executable, profile, request.workspace or request.cwd,
                            request.env, list(request.read_paths))
        marked = inheritance.with_marker(request, profile)
        return run_wrapped(self.name, self.isolation, prefix, marked, limits,
                           subprocess_module=self._subprocess)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
