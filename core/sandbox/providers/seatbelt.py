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

import glob
import os
import subprocess
import tempfile
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

#: The machine's shared temporary tree. It is granted only to a profile that
#: asks for it (``shared_tmp``) and only to a writable profile: a tool that
#: stages an intermediate file at a hardcoded ``/tmp/<name>`` path ignores
#: the child's TMPDIR and fails without the real tree. The paths are listed
#: both resolved and in their macOS symlink form, because the backend that
#: writes the file may name it either way.
_SHARED_TMP = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp")

#: Entries inside ``.git`` that stay read-only even in a writable profile.
#: The rest of the directory is writable, because staging and committing are
#: ordinary project work: ``git add`` writes the index, the object files and
#: the refs. These entries are different in kind - git EXECUTES hooks, and the
#: config can name a program to run (``core.fsmonitor``, ``core.hooksPath``,
#: a ``!`` alias), so a run that could write them would gain execution on the
#: next command the platform runs in that repository.
_PROTECTED_GIT_ENTRIES = ("hooks", "config", "modules")


#: Files in the installation root that carry credentials or private state.
#: They are denied individually: the root also holds the skills, the shipped
#: defaults and the dialog history, which a run may legitimately read.
_SECRET_GLOBS = (
    "*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3",
    "*.pem", "*.key", "*.p12", "*.pfx",
    ".env", ".env.*",
)
#: Folders in the installation root that hold credentials or private state.
_SECRET_DIRS = (
    "personal", "connectors", "keys", "certs", "services", "system_prompts",
)


def user_temp_dirs() -> List[str]:
    """Return the system-wide temporary trees of the current user.

    A program that stages an intermediate file does not always honour
    ``TMPDIR``: the macOS frameworks ask the system for the per-user temp
    directory (``/var/folders/<xx>/<hash>/T``), which no environment
    variable controls, and a converter that writes there fails inside a
    sandbox that only granted ``/tmp``. The directory is derived from this
    process's own temporary directory by walking up to the ``T`` component,
    so the value matches what the child's framework calls will resolve to
    on this machine.

    Returns:
        Zero or more absolute paths, deduplicated. An unresolved location
        yields an empty list: a guess must never widen the write grant.
    """
    found: List[str] = []
    try:
        current = _real(tempfile.gettempdir())
    except Exception:  # pragma: no cover - a broken temp dir is not fatal
        return found
    for _ in range(8):
        parent, name = os.path.split(current)
        if not parent or parent == current:
            break
        if name == "T":
            found.append(current)
            break
        current = parent
    return found


def protected_git_paths(workspace: str) -> List[str]:
    """Return the ``.git`` entries a run may never write, even in its project.

    Only entries that exist are returned: a path with nothing behind it needs
    no rule, and naming it would make the generated policy claim a protection
    it cannot demonstrate. A workspace that is not a repository yields an
    empty list.

    Args:
        workspace: project root a run was given.
    """
    if not workspace:
        return []
    git_dir = os.path.join(_real(workspace), ".git")
    if not os.path.isdir(git_dir):
        return []
    found = []
    for name in _PROTECTED_GIT_ENTRIES:
        candidate = os.path.join(git_dir, name)
        if os.path.exists(candidate):
            found.append(candidate)
    return found


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
    if profile.shared_tmp and profile.writable:
        allowed += [_real(p) for p in _SHARED_TMP]
        allowed += user_temp_dirs()
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
    # list above: the repository hooks and config stay the platform's - git
    # executes them - while the index, objects and refs stay writable, so a
    # run can stage and commit the change it was asked to make. The secrets
    # on the same disk stay unreadable.
    blocked_writes = protected_git_paths(workspace)
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
    the user's SSH keys.

    The installation root is NOT denied as a whole. It holds the skill
    packages, the shipped defaults, the projects under ``apps`` and the
    dialog history - the very files a run is asked to work with. Denying the
    directory wholesale made skill scripts unrunnable and the dialog folder
    unreadable, so only the files and folders that actually carry
    credentials are listed here.

    A path that lies inside the workspace is skipped - the project is what
    the run was asked to inspect, and denying it would make the profile
    unusable when the platform itself is the project. The same holds for a
    path that CONTAINS the workspace: the run needs the folder it was given,
    and the files below it stay protected individually.
    """
    candidates: List[str] = []
    try:
        from core.paths import DATA_DIR

        base = str(DATA_DIR)
        for pattern in _SECRET_GLOBS:
            candidates.extend(sorted(glob.glob(os.path.join(base, pattern))))
        for name in _SECRET_DIRS:
            candidates.append(os.path.join(base, name))
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
        if workspace_real and workspace_real.startswith(real + os.sep):
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
        """Execute *request* inside the generated profile.

        A run that already executes inside a sandbox is NOT wrapped again:
        the operating system refuses a nested ``sandbox_apply``, and the
        enclosing policy is the only one the kernel can still enforce. The
        grants of the two policies are compared first, so a run that needs
        more than the enclosing policy gives is refused with an explanation
        instead of dying inside the wrapper.
        """
        from core.sandbox import inheritance

        enclosing = inheritance.enclosing_sandbox(request.env)
        if enclosing is not None:
            verdict = inheritance.compare(enclosing, profile)
            if not verdict.ok:
                return SandboxResult(ok=False, provider=self.name,
                                     isolation=enclosing.isolation,
                                     denied=True, error=verdict.error)
            # Already confined by the outer policy: run the program directly
            # and report the isolation that is actually in force.
            return run_wrapped(self.name, enclosing.isolation, [], request,
                               limits, subprocess_module=self._subprocess)
        policy = build_profile(profile, request.workspace or request.cwd,
                               _writable_paths(request))
        prefix = [SANDBOX_EXEC, "-p", policy, "--"]
        marked = inheritance.with_marker(request, profile)
        return run_wrapped(self.name, self.isolation, prefix, marked, limits,
                           subprocess_module=self._subprocess)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
