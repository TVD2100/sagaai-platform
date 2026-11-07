# -*- coding: utf-8 -*-
"""Apply the resource limits of a profile before the child program starts.

A ceiling written in a dataclass is a promise, not a bound: the kernel has
to receive it, and it has to arrive BEFORE the generated program runs,
otherwise there is a window in which it can allocate or write freely.

``preexec_fn`` would be the direct route, but it is unsafe in a
multi-threaded parent (the platform runs several threads, and Python
documents ``preexec_fn`` as unsafe there). The limits are therefore applied
by a small launcher script that bounds itself and then ``exec``s the real
argv - the bounds survive the exec, so they cover the program itself and
not merely a wrapper that spawned it.

Only ceilings the kernel can enforce per process are listed:

* ``RLIMIT_CPU`` and ``RLIMIT_FSIZE`` work on every POSIX system;
* ``RLIMIT_AS`` is applied on Linux only - macOS rejects a ceiling below
  the virtual size the process has already mapped, so a run would fail
  instead of being bounded;
* ``max_processes`` is NOT applied: ``RLIMIT_NPROC`` counts every process
  of the same user, so a per-run ceiling would be measured against the
  whole desktop and block even a legitimate ``subprocess.run`` from the
  snippet. The process-tree ceiling belongs to the container provider,
  where ``--pids-limit`` counts inside the sandbox.

A ceiling the kernel refuses never fails the run: it is skipped and the
remaining ones still apply, because an unsupported limit on one machine
must not make the execution tool unusable there.
"""
from __future__ import annotations

import json
import sys
from typing import Dict, List, Optional, Sequence

from core.sandbox.contracts import ResourceLimits

#: The launcher: bound itself, then replace itself with the real program.
_LAUNCHER_CODE = '''import json, os, sys

try:
    import resource
except ImportError:  # Windows has no rlimit mechanism
    resource = None

spec = json.loads(sys.argv[1])["limits"]
if resource is not None:
    for _name, _value in spec.items():
        _res = getattr(resource, _name, None)
        if _res is None or not _value:
            continue
        try:
            _soft, _hard = resource.getrlimit(_res)
            _target = int(_value)
            if _hard not in (resource.RLIM_INFINITY, -1) and _target > _hard:
                _target = _hard
            resource.setrlimit(_res, (_target, _target))
        except (ValueError, OSError):
            # An unsupported ceiling is skipped, never fatal.
            continue

try:
    os.execvp(sys.argv[3], sys.argv[3:])
except OSError as exc:
    sys.stderr.write("sandbox launcher: " + str(exc) + chr(10))
    raise SystemExit(127)
'''


def limits_spec(limits: ResourceLimits,
                platform: Optional[str] = None) -> Dict[str, int]:
    """Return the kernel limits that *limits* may enforce on *platform*.

    Args:
        limits: the ceilings granted by the profile.
        platform: value of ``sys.platform`` to plan for; defaults to the
            running one. Tests pass an explicit value to pin both branches.

    Returns:
        ``{resource name: ceiling}`` for every ceiling this platform can
        actually hold. An empty mapping means there is nothing to wrap.
    """
    target_platform = str(platform if platform is not None else sys.platform)
    spec: Dict[str, int] = {}
    if target_platform.startswith("win"):
        # Windows has no rlimit mechanism: wrapping the run in an extra
        # interpreter would only add startup cost without bounding it.
        return spec
    if limits.cpu_seconds:
        spec["RLIMIT_CPU"] = int(limits.cpu_seconds)
    if limits.max_file_bytes:
        spec["RLIMIT_FSIZE"] = int(limits.max_file_bytes)
    if limits.memory_mb and target_platform.startswith("linux"):
        spec["RLIMIT_AS"] = int(limits.memory_mb) * 1024 * 1024
    return spec


def wrap(argv: Sequence[str], limits: ResourceLimits,
         python: Optional[str] = None,
         platform: Optional[str] = None) -> List[str]:
    """Return an argv that applies *limits* and then runs *argv*.

    The launcher is skipped when there is nothing to enforce: an extra
    interpreter in front of a run costs startup time and hides the real
    command from the log for no benefit.

    Args:
        argv: the program to run, already wrapped by a sandbox provider.
        limits: ceilings granted by the profile.
        python: interpreter used for the launcher (defaults to this one).
        platform: platform to plan for; defaults to the running one.
    """
    spec = limits_spec(limits, platform=platform)
    if not spec:
        return list(argv)
    return [python or sys.executable, "-c", _LAUNCHER_CODE,
            json.dumps({"limits": spec}), "--"] + list(argv)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
