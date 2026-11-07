# -*- coding: utf-8 -*-
"""T1 provider on Windows: the program runs inside WSL2.

Windows has no ``bwrap`` and no ``sandbox-exec``. What it does have is a
Linux distribution: a generated program started inside it is confined the
same way a Linux child is - it cannot see the Windows file system beyond
the drives the distribution mounts, and it has no way to reach a Windows
handle.

The request describes a HOST command: a Windows interpreter path and a
script under the Windows temporary directory. Inside the distribution those
paths mean nothing, so the provider translates them the same way the
container provider does, and refuses a command it cannot translate rather
than running a different program.

Network removal is not a given. ``unshare -n`` needs the kernel feature and
the permission, and a distribution that forbids it keeps the network even
while the profile promises otherwise. The provider therefore probes that
one capability and declares ``network-off`` only when the probe succeeded:
the policy then either finds a provider that can honour the profile or
refuses the run - it never weakens the promise silently.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any, List, Optional

from core.sandbox.contracts import ResourceLimits, SandboxProfile, SandboxRequest, SandboxResult
from core.sandbox.policy import SandboxProvider
from core.sandbox.providers._spawn import run_wrapped

#: Name reported in a result and stored in the journal.
PROVIDER_NAME = "wsl"

#: Windows drive path, e.g. ``C:\\Users\\...``.
_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def _resolve_wsl() -> Optional[str]:
    """Return the WSL launcher, or None when this machine has none."""
    return shutil.which("wsl") or shutil.which("wsl.exe")


def _is_host_python(item: str) -> bool:
    """True when *item* is a Python interpreter of the Windows host."""
    base = os.path.basename(str(item or "").replace("\\", "/")).lower()
    return base.startswith("python") or base in ("py", "py.exe", "python.exe")


def to_wsl_path(path: str) -> str:
    """Translate a Windows path into the path the distribution sees.

    Only the drive form is translated. A path that is already POSIX, or one
    the provider does not recognise, is returned as it is: guessing would
    point the child at a file nobody asked for.
    """
    text = str(path or "")
    match = _DRIVE_RE.match(text)
    if match:
        drive = match.group(1).lower()
        rest = match.group(2).replace("\\", "/")
        return "/mnt/%s/%s" % (drive, rest)
    if text.startswith("\\\\wsl$\\") or text.startswith("\\\\wsl.localhost\\"):
        parts = text.split("\\")
        return "/" + "/".join(part for part in parts[3:] if part)
    return text


def untranslatable(argv: List[str]) -> str:
    """Return the first argument the distribution cannot be given, or "".

    A drive path and a ``\\\\wsl$`` path are translated; a relative word is
    the same on both sides. An absolute path of another form - a network
    share, for instance - has no counterpart inside the distribution, and
    passing it through would make the child look for a file that does not
    exist there. The run is refused instead: the analysis covered one
    program, and only that program may be started.
    """
    for item in list(argv)[1:]:
        text = str(item or "")
        if not text:
            continue
        if _DRIVE_RE.match(text) or text.startswith("\\\\wsl"):
            continue
        if text.startswith("\\\\") or (len(text) > 1 and text[1] == ":"):
            return text
    return ""


def translate_argv(argv: List[str]) -> List[str]:
    """Return the command as the distribution will see it.

    The interpreter becomes ``python3`` and every absolute Windows path is
    translated. Anything else is passed through unchanged, because a flag
    or a plain word means the same on both sides.
    """
    items = [str(item) for item in argv]
    if not items:
        return items
    result = ["python3"] if _is_host_python(items[0]) else [items[0]]
    for item in items[1:]:
        if _DRIVE_RE.match(item) or item.startswith("\\\\wsl"):
            result.append(to_wsl_path(item))
        else:
            result.append(item)
    return result


def build_argv(wsl: str, profile: SandboxProfile, request: SandboxRequest,
               isolate_network: bool) -> List[str]:
    """Return the ``wsl`` argv that implements *profile*.

    Args:
        wsl: path of the launcher.
        profile: the profile being honoured.
        request: the prepared run; its paths are translated for Linux.
        isolate_network: True only when the distribution proved it can drop
            the network namespace; without that proof the flag is omitted and
            the capability is not claimed.
    """
    workspace = to_wsl_path(request.workspace or "")
    workdir = to_wsl_path(request.cwd or "") or workspace or "/"
    # ``--cd`` is an option of the LAUNCHER, so it must come before
    # ``--exec``; after it the words belong to the program being run.
    argv = [wsl, "--cd", workdir, "--exec"]
    if isolate_network and not profile.network:
        argv += ["unshare", "-n", "--"]
    return argv + translate_argv(list(request.argv))


class WslProvider(SandboxProvider):
    """Run a request inside the default WSL2 distribution."""

    name = PROVIDER_NAME
    isolation = "os"

    def __init__(self, subprocess_module: Any = None) -> None:
        """Store the spawn module; it is a test seam."""
        self._subprocess = subprocess_module
        self._probe: Optional[bool] = None
        self._network_probe: Optional[bool] = None

    @property
    def capabilities(self):
        """What this distribution really enforces, not what it promises.

        ``write-confined`` holds: the distribution sees the mounted drives,
        and a profile that grants writes makes the workspace writable. The
        network is a separate probe, so the capability is claimed only when
        ``unshare -n`` actually worked.
        """
        declared = ["write-confined"]
        if self._can_isolate_network():
            declared.append("network-off")
        return tuple(declared)

    def _can_isolate_network(self) -> bool:
        """Return True when the distribution can drop the network namespace."""
        if self._network_probe is not None:
            return self._network_probe
        self._network_probe = False
        executable = _resolve_wsl()
        if not executable:
            return False
        try:
            completed = subprocess.run(
                [executable, "--exec", "unshare", "-n", "--", "/bin/true"],
                capture_output=True, timeout=25,
            )
            self._network_probe = completed.returncode == 0
        except Exception:
            self._network_probe = False
        return self._network_probe

    def available(self) -> bool:
        """True when WSL exists and its distribution has Python 3."""
        if self._probe is not None:
            return self._probe
        self._probe = False
        executable = _resolve_wsl()
        if not executable:
            return False
        try:
            completed = subprocess.run(
                [executable, "--exec", "python3", "--version"],
                capture_output=True, timeout=30,
            )
            self._probe = completed.returncode == 0
        except Exception:
            self._probe = False
        return self._probe

    def run(self, request: SandboxRequest, profile: SandboxProfile,
            limits: ResourceLimits) -> SandboxResult:
        """Execute *request* inside the WSL2 distribution."""
        executable = _resolve_wsl()
        if not executable:
            return SandboxResult(ok=False, provider=self.name, isolation=self.isolation,
                                 error="WSL is not installed on this machine.")
        if not request.argv:
            return SandboxResult(ok=False, provider=self.name, isolation=self.isolation,
                                 error="Empty command: nothing to run.")
        blocked = untranslatable(list(request.argv))
        if blocked:
            return SandboxResult(ok=False, provider=self.name,
                                 isolation=self.isolation,
                                 error=("The command names a path the WSL "
                                        "distribution cannot reach: "
                                        f"{blocked}. The run was refused "
                                        "instead of being started against "
                                        "a different file."))
        argv = build_argv(executable, profile, request, self._can_isolate_network())
        return run_wrapped(self.name, self.isolation, [], request, limits,
                           subprocess_module=self._subprocess, argv=argv)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
