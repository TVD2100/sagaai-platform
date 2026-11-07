# -*- coding: utf-8 -*-
"""T1 provider on macOS: the Seatbelt sandbox (``sandbox-exec``).

The policy is expressed as an SBPL profile generated from the sandbox
profile: the process starts from ``(allow default)`` and then loses exactly
what the profile does not grant - network and, when the profile is not
writable, every write outside the temporary area.

Write access is granted by PATH, and paths are resolved with ``realpath``:
``/tmp`` and ``/var`` are symbolic links on macOS, so an unresolved path in
a ``subpath`` rule silently matches nothing and the child loses access to
its own temporary directory.

The provider is only offered when the wrapper is present AND a probe run
succeeds: an installed ``sandbox-exec`` that cannot start a child must not be
reported as a working isolation level.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, List, Optional

from core.sandbox.contracts import ResourceLimits, SandboxProfile, SandboxRequest, SandboxResult
from core.sandbox.policy import SandboxProvider
from core.sandbox.providers._spawn import run_wrapped

#: Name reported in a result and stored in the journal.
PROVIDER_NAME = "seatbelt"
#: The wrapper that applies the profile.
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

#: Paths the child always needs to write to, beyond the workspace itself.
#: Only device nodes are unconditional: the shared temporary trees (/tmp,
#: /var/folders) hold other programs' files, and granting them whole would
#: let one run rewrite a neighbour's temporary data. The child's own HOME
#: and TMPDIR are granted separately, from its environment.
_ALWAYS_WRITABLE = ("/dev",)


def _real(path: str) -> str:
    """Return the resolved absolute path, or the input when it cannot be."""
    try:
        return os.path.realpath(str(path))
    except OSError:  # pragma: no cover - defensive
        return str(path)


def _escape(path: str) -> str:
    """Escape a path for an SBPL string literal."""
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def build_profile(profile: SandboxProfile, workspace: str,
                  writable_paths: Optional[List[str]] = None) -> str:
    """Return the SBPL text for *profile*.

    Args:
        profile: the sandbox profile being honoured.
        workspace: the project root; it is writable only for a writable
            profile, so a read-only run cannot modify the project.
        writable_paths: extra paths the run needs (the child's HOME and
            TMPDIR, which live outside the project).
    """
    rules = ["(version 1)", "(allow default)"]
    if not profile.network:
        rules.append("(deny network*)")
    # Writes are denied everywhere and then re-allowed by path. A read-only
    # profile simply does not include the workspace in the allowed set: the
    # temporary area stays writable because the interpreter itself needs it.
    allowed = [_real(p) for p in _ALWAYS_WRITABLE]
    allowed += [_real(p) for p in (writable_paths or [])]
    if profile.writable and workspace:
        allowed.insert(0, _real(workspace))
    rules.append("(deny file-write*)")
    if allowed:
        # An empty allow list would leave a rule with no path, which the
        # policy parser rejects outright; with nothing to allow the blanket
        # denial above is already the whole answer.
        rules.append("(allow file-write* " +
                     " ".join('(subpath "%s")' % _escape(p) for p in allowed) + ")")
    # The last matching rule wins, so the denials below override the allow
    # list above: the repository metadata stays the platform's, and the
    # secrets on the same disk stay unreadable.
    blocked_writes = [os.path.join(_real(workspace), ".git")] if workspace else []
    if blocked_writes:
        rules.append("(deny file-write* " +
                     " ".join('(subpath "%s")' % _escape(p) for p in blocked_writes) + ")")
    blocked_reads = sensitive_reads(workspace)
    if blocked_reads:
        rules.append("(deny file-read* " +
                     " ".join('(subpath "%s")' % _escape(p) for p in blocked_reads) + ")")
    return "\n".join(rules)


def _writable_paths(request: SandboxRequest) -> List[str]:
    """Return the child's own HOME and TMPDIR, when it has them."""
    paths = []
    for key in ("HOME", "TMPDIR", "TMP", "TEMP"):
        value = request.env.get(key)
        if value:
            paths.append(value)
    return paths


def sensitive_reads(workspace: str) -> List[str]:
    """Return paths the child must not read even inside a writable profile.

    A filtered environment hides the provider keys from the child, but the
    files that hold them are still on the same disk: without this list the
    sandbox would happily let generated code open the platform database or
    the user's SSH keys. A path that lies inside the workspace is skipped -
    the project is what the run was asked to inspect, and denying it would
    make the profile unusable when the platform itself is the project.
    """
    candidates = []
    try:
        from core.paths import DATA_DIR

        candidates.append(str(DATA_DIR))
    except Exception:  # pragma: no cover - a headless run has no data dir
        pass
    home = os.path.expanduser("~")
    for name in (".ssh", ".aws", ".gnupg", ".netrc", ".config/gcloud", "Library/Keychains"):
        candidates.append(os.path.join(home, name))

    workspace_real = _real(workspace) if workspace else ""
    result = []
    for path in candidates:
        real = _real(path)
        if not real or not os.path.exists(real):
            continue
        if workspace_real and (real == workspace_real or
                               real.startswith(workspace_real + os.sep)):
            continue
        result.append(real)
    return result


class SeatbeltProvider(SandboxProvider):
    """Run a request under a generated Seatbelt profile."""

    name = PROVIDER_NAME
    isolation = "os"
    #: SBPL rules deny network traffic and confine writes by path, so both
    #: guarantees the profiles ask for can be honoured by this backend.
    capabilities = ("network-off", "write-confined")

    def __init__(self, subprocess_module: Any = None) -> None:
        """Store the spawn module; ``None`` means the real ``subprocess``."""
        self._subprocess = subprocess_module
        self._probe: Optional[bool] = None

    def available(self) -> bool:
        """True when the wrapper exists and can start a trivial child."""
        if self._probe is not None:
            return self._probe
        self._probe = False
        if not os.path.exists(SANDBOX_EXEC):
            return False
        try:
            completed = subprocess.run(
                [SANDBOX_EXEC, "-p", "(version 1)(allow default)", "--",
                 "/bin/sh", "-c", "true"],
                capture_output=True, timeout=15,
            )
            self._probe = completed.returncode == 0
        except Exception:
            self._probe = False
        return self._probe

    def run(self, request: SandboxRequest, profile: SandboxProfile,
            limits: ResourceLimits) -> SandboxResult:
        """Execute *request* inside the generated profile."""
        policy = build_profile(profile, request.workspace or request.cwd,
                               _writable_paths(request))
        prefix = [SANDBOX_EXEC, "-p", policy, "--"]
        return run_wrapped(self.name, self.isolation, prefix, request, limits,
                           subprocess_module=self._subprocess)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
