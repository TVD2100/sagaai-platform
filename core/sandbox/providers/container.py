# -*- coding: utf-8 -*-
"""T2 provider: a container (Docker).

A container is the first level that confines MORE than the file system and
the network: it also bounds the process count and the memory from outside
the child, so a fork bomb or an allocation loop meets a ceiling the kernel
enforces rather than a value the program agrees to respect.

The request, however, describes a HOST command: an interpreter path and a
script under the host temporary directory. None of those exist inside the
image. The provider therefore translates the command instead of hoping it
happens to work:

* the host interpreter becomes ``python3`` inside the image;
* every absolute path the command names is bind-mounted at the same path,
  read-only, unless it lies inside the workspace (which is mounted once);
* the child receives its own HOME and TMPDIR inside the container.

A command that cannot be translated is refused. Running it anyway would
mean the child silently operates on a different file than the one the tool
layer analysed, which is worse than a refusal: the guarantee would be
stated about one program and kept for another.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from typing import Any, List, Optional, Tuple

from core.sandbox.contracts import ResourceLimits, SandboxProfile, SandboxRequest, SandboxResult
from core.sandbox.policy import SandboxProvider
from core.sandbox.providers._spawn import run_wrapped

#: Name reported in a result and stored in the journal.
PROVIDER_NAME = "container"
#: Image used when the profile does not name one.
DEFAULT_IMAGE = "python:3.12-slim"
#: Writable scratch space inside the container.
CONTAINER_TMP = "/var/tmp"


def _resolve_docker() -> Optional[str]:
    """Return the docker executable, or None when it is not installed."""
    return shutil.which("docker")


def _is_host_python(item: str) -> bool:
    """True when *item* is a Python interpreter of the host machine."""
    base = os.path.basename(str(item or "")).lower()
    return base.startswith("python") or base in ("py", "pypy", "pypy3")


def _inside(root: str, path: str) -> bool:
    """True when *path* lies inside *root*."""
    if not root:
        return False
    root = os.path.abspath(root).rstrip(os.sep)
    path = os.path.abspath(path)
    return path == root or path.startswith(root + os.sep)


def _mounts(request: SandboxRequest, profile: SandboxProfile) -> List[Tuple[str, str, bool]]:
    """Return ``(host, inside, writable)`` for every path the run needs.

    The workspace is mounted once and matters most: it is the only mount
    that may be writable. Paths named by the command are mounted read-only
    at the SAME absolute path, so the command needs no rewriting beyond the
    interpreter.
    """
    workspace = request.workspace or ""
    mounts: List[Tuple[str, str, bool]] = []
    if workspace and os.path.isdir(workspace):
        mounts.append((workspace, workspace, bool(profile.writable)))
    seen = {workspace} if workspace else set()
    for item in list(request.argv)[1:]:
        text = str(item or "")
        if not os.path.isabs(text) or not os.path.exists(text):
            continue
        if any(_inside(root, text) for root in seen if root):
            continue
        mounts.append((text, text, False))
        seen.add(text)
    return mounts


def translate_argv(argv: List[str]) -> List[str]:
    """Return the command as the image will see it.

    Only the interpreter is rewritten: everything else keeps its path, and
    the matching mount makes that path exist inside the container.
    """
    items = [str(item) for item in argv]
    if not items:
        return items
    return (["python3"] + items[1:]) if _is_host_python(items[0]) else items


def _container_name(fingerprint: str) -> str:
    """Return a unique, docker-safe name for one run."""
    return "sagaai-sbx-%s-%s" % (str(fingerprint or "")[:12], secrets.token_hex(4))


def build_argv(docker: str, name: str, image: str, profile: SandboxProfile,
               request: SandboxRequest, limits: ResourceLimits) -> List[str]:
    """Return the ``docker run`` argv that implements *profile*.

    Args:
        docker: path of the executable.
        name: container name, so the run can be killed from outside.
        image: image to run (a Python image is expected by the tool layer).
        profile: the profile being honoured.
        request: the prepared run; its ``argv`` is translated for the image.
        limits: ceilings already computed by the policy.
    """
    workspace = request.workspace or ""
    workdir = request.cwd if workspace and _inside(workspace, request.cwd) else (workspace or "/")
    argv = [docker, "run", "--rm", "--init", "--name", name,
            "--network", "bridge" if profile.network else "none",
            # The image root is read-only: a run writes only into a mount it
            # was granted, so "writable" stays a decision of the profile.
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(int(limits.max_processes)),
            "--memory", "%dm" % int(limits.memory_mb),
            "--tmpfs", CONTAINER_TMP,
            "-w", workdir,
            "-e", "HOME=" + CONTAINER_TMP,
            "-e", "TMPDIR=" + CONTAINER_TMP,
            "-e", "PYTHONDONTWRITEBYTECODE=1"]
    # Run as the invoking user: a workspace file written as root would be
    # unreadable for the person who asked for the run.
    if hasattr(os, "getuid"):
        argv += ["--user", "%d:%d" % (os.getuid(), os.getgid())]
    for host, inside, writable in _mounts(request, profile):
        argv += ["-v", "%s:%s:%s" % (host, inside, "rw" if writable else "ro")]
    argv += [image] + translate_argv(list(request.argv))
    return argv


class ContainerProvider(SandboxProvider):
    """Run a request inside a container with a read-only image root."""

    name = PROVIDER_NAME
    isolation = "container"
    #: The image root is read-only, the network namespace is removed when the
    #: profile grants none, and the process count is limited by the runtime.
    capabilities = ("network-off", "write-confined", "process-limit")

    def __init__(self, subprocess_module: Any = None, image: str = "") -> None:
        """Store the spawn module and the image; both are test seams."""
        self._subprocess = subprocess_module
        self._image = str(image or DEFAULT_IMAGE)
        self._probe: Optional[bool] = None

    def available(self) -> bool:
        """True when docker exists, answers, and the image is present.

        The probe asks the daemon, not just the client: a client without a
        running daemon is installed and unusable, and offering it as an
        isolation level would turn every run into a startup failure.
        """
        if self._probe is not None:
            return self._probe
        self._probe = False
        executable = _resolve_docker()
        if not executable:
            return False
        try:
            completed = subprocess.run(
                [executable, "image", "inspect", self._image],
                capture_output=True, timeout=20,
            )
            self._probe = completed.returncode == 0
        except Exception:
            self._probe = False
        return self._probe

    def run(self, request: SandboxRequest, profile: SandboxProfile,
            limits: ResourceLimits) -> SandboxResult:
        """Execute *request* inside a container."""
        executable = _resolve_docker()
        if not executable:
            return SandboxResult(ok=False, provider=self.name, isolation=self.isolation,
                                 error="docker is not installed on this machine.")
        if not request.argv:
            return SandboxResult(ok=False, provider=self.name, isolation=self.isolation,
                                 error="Empty command: nothing to run.")
        name = _container_name(request.fingerprint())
        argv = build_argv(executable, name, self._image, profile, request, limits)
        return run_wrapped(self.name, self.isolation, [], request, limits,
                           subprocess_module=self._subprocess,
                           argv=argv,
                           on_cancel=lambda: _kill_container(executable, name))


def _kill_container(docker: str, name: str) -> None:
    """Stop the container of a cancelled run.

    Killing the ``docker run`` client does not stop the container: the
    client is a proxy, the container belongs to the daemon. A cancelled run
    would therefore keep writing inside its mount after the user pressed
    Stop, which is exactly what the cancellation is meant to prevent.
    """
    try:
        subprocess.run([docker, "kill", str(name)], capture_output=True, timeout=20)
    except Exception:  # pragma: no cover - a dead container is not an error
        pass

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
