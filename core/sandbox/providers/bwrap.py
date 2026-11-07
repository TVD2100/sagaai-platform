# -*- coding: utf-8 -*-
"""T1 provider on Linux: bubblewrap (``bwrap``).

``bwrap`` builds a mount namespace for the child: the root filesystem is
mounted read-only, a private temporary directory is provided, the workspace
is bind-mounted read-write only when the profile is writable, and
``--unshare-net`` removes the network namespace entirely when the profile
does not grant it.

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
from typing import Any, List, Optional

from core.sandbox.contracts import ResourceLimits, SandboxProfile, SandboxRequest, SandboxResult
from core.sandbox.policy import SandboxProvider
from core.sandbox.providers._spawn import run_wrapped

#: Name reported in a result and stored in the journal.
PROVIDER_NAME = "bwrap"


def _resolve_bwrap() -> Optional[str]:
    """Return the bwrap executable, or None when it is not installed."""
    return shutil.which("bwrap")


def build_argv(bwrap: str, profile: SandboxProfile, workspace: str) -> List[str]:
    """Return the bwrap arguments that implement *profile*.

    Args:
        bwrap: path of the executable.
        profile: the profile being honoured.
        workspace: project root mounted into the namespace.
    """
    argv = [bwrap, "--die-with-parent", "--new-session",
            "--ro-bind", "/", "/",
            "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp"]
    if not profile.network:
        argv.append("--unshare-net")
    if workspace:
        argv += ["--bind" if profile.writable else "--ro-bind", workspace, workspace]
        argv += ["--chdir", workspace]
    # Mounts are applied in order, so a later mask hides what the earlier
    # root bind made visible: the platform data directory and the user's key
    # stores are the files a generated program has no reason to see.
    from core.sandbox.providers.seatbelt import sensitive_reads

    for blocked in sensitive_reads(workspace):
        argv += ["--tmpfs", blocked]
    if workspace and profile.writable:
        git_dir = os.path.join(workspace, ".git")
        if os.path.isdir(git_dir):
            argv += ["--ro-bind", git_dir, git_dir]
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
        """Execute *request* inside a bubblewrap namespace."""
        executable = _resolve_bwrap()
        if not executable:
            return SandboxResult(ok=False, provider=self.name, isolation=self.isolation,
                                 error="bwrap is not installed on this machine.")
        prefix = build_argv(executable, profile, request.workspace or request.cwd)
        return run_wrapped(self.name, self.isolation, prefix, request, limits,
                           subprocess_module=self._subprocess)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
