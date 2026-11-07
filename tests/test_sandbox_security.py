# -*- coding: utf-8 -*-
"""tests/test_sandbox_security.py - the guarantees of Phase 2 under attack.

Every test here asserts a guarantee that a profile PROMISES and a provider
claims to enforce. They are written as attacks, not as happy paths:

* the child tries to write outside the workspace, directly and through a
  symbolic link planted inside it;
* the child tries to open a socket when the profile grants no network;
* the child tries to write a file larger than the disk ceiling;
* the child tries to burn more CPU than the profile allows;
* a running child is stopped from another thread, the way the Stop button
  stops it.

The provider-dependent tests skip when this machine has no provider at the
claimed level: a guarantee is only meaningful where it can be measured, and
a silent pass on a machine without the backend would be worse than a skip.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.sandbox import active, launcher, profiles, runner
from core.sandbox.contracts import ResourceLimits


def _providers():
    """Return the default providers of this machine."""
    from core.sandbox.providers import default_providers

    return default_providers()


def _os_provider():
    """Return an available provider at 'os' level, or None."""
    from core.sandbox.policy import select_provider

    profile = profiles.get_profile(profiles.WORKSPACE_WRITE)
    return select_provider(profile, _providers())


requires_os = pytest.mark.skipif(
    _os_provider() is None,
    reason="no provider reaches 'os' isolation on this machine",
)


@pytest.fixture()
def zone(tmp_path):
    """A workspace, a separate home and the environment a child receives."""
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    return workspace, home, {"PATH": "/usr/bin:/bin", "HOME": str(home),
                             "TMPDIR": str(home)}


def _run(zone, code: str, profile: str = profiles.WORKSPACE_WRITE,
         thread_id: str = "security-test"):
    """Run *code* through the public sandbox path under *profile*."""
    workspace, _home, env = zone
    request = runner.build_request(
        tool="run_code", argv=[sys.executable, "-c", code], cwd=str(workspace),
        env=env, profile=profile, thread_id=thread_id, workspace=str(workspace),
    )
    return runner.execute(request, profile=profile)


# ── file system ─────────────────────────────────────────────────────────────


def test_a_writable_profile_can_write_inside_the_workspace(zone):
    """The workspace stays usable: the sandbox confines writes, not forbids."""
    workspace, _home, _env = zone
    result = _run(zone, "open('inside.txt', 'w').write('ok'); print('WROTE')")

    assert result.ok is True, result.error
    assert (workspace / "inside.txt").read_text() == "ok"


@requires_os
def test_a_writable_profile_cannot_write_outside_the_workspace(zone):
    """A path outside the workspace is refused by the OS, not by convention.

    The probe targets the REAL home of the platform user, not the temporary
    home handed to the child: the child's own home is granted on purpose
    (an interpreter writes caches there), so only a path outside every
    granted zone shows whether the confinement is real.
    """
    target = Path.home() / ("sagaai-escape-%s.txt" % os.getpid())
    if target.exists():  # pragma: no cover - a leftover from a local run
        target.unlink()
    try:
        result = _run(zone, "open(%r, 'w').write('x')" % str(target))

        assert result.ok is False
        assert not target.exists(), "the write escaped the workspace"
    finally:
        if target.exists():  # pragma: no cover - only when the check failed
            target.unlink()


@requires_os
def test_a_symbolic_link_inside_the_workspace_does_not_widen_it(zone):
    """A link is resolved by the kernel before the rule is checked.

    This is the bypass that a path-prefix check in Python cannot catch: the
    child is allowed to write under the workspace, and the link turns that
    permission into a write anywhere the platform user can reach.
    """
    workspace, _home, _env = zone
    outside = Path.home()
    probe = outside / ("sagaai-link-%s.txt" % os.getpid())
    trojan = workspace / "escape"
    try:
        os.symlink(str(outside), str(trojan))
    except (OSError, NotImplementedError):  # pragma: no cover - platform
        pytest.skip("symbolic links are not available here")

    try:
        result = _run(zone, "open('escape/%s', 'w').write('x')" % probe.name)

        assert result.ok is False
        assert not probe.exists(), "the link widened the write zone"
    finally:
        if probe.exists():  # pragma: no cover - only when the check failed
            probe.unlink()


@requires_os
def test_a_writable_profile_cannot_write_into_git_metadata(zone):
    """Generated code must not be able to rewrite the repository history.

    The workspace is writable on purpose, and ``.git`` sits inside it: a
    plain subpath grant would let a run stage commits, rewrite hooks or plant
    a config that runs on the next platform command. The profile keeps the
    project editable and the repository metadata read-only.
    """
    workspace, _home, _env = zone
    git_dir = workspace / ".git"
    git_dir.mkdir()
    head = git_dir / "HEAD"
    head.write_text("ref: refs/heads/main\n")

    result = _run(zone, "open('.git/HEAD', 'w').write('pwned')")

    assert result.ok is False
    assert head.read_text() == "ref: refs/heads/main\n"


@requires_os
def test_the_platform_data_directory_is_unreadable_from_a_run(zone):
    """The provider keys survive the run: the child cannot open the data dir.

    The environment is filtered, but the data directory sits on the same
    disk as the project. Listing it is the cheapest probe a hostile program
    would try first, so it is the one this test makes.
    """
    from core.paths import DATA_DIR

    if not Path(DATA_DIR).exists():  # pragma: no cover - headless install
        pytest.skip("this install has no data directory")

    result = _run(zone, "import os; print(os.listdir(%r))" % str(DATA_DIR))

    assert result.ok is False
    assert "sagaai.db" not in result.stdout


def test_sensitive_reads_protects_the_data_directory(zone):
    """The list is built from the install, not hard-coded per machine."""
    from core.paths import DATA_DIR
    from core.sandbox.providers.seatbelt import sensitive_reads

    workspace, _home, _env = zone
    blocked = sensitive_reads(str(workspace))

    assert str(Path(DATA_DIR).resolve()) in blocked


def test_sensitive_reads_leaves_a_workspace_that_contains_the_data(zone):
    """A platform run that targets the install itself is not blinded.

    Denying a path that lies inside the project would make the sandbox
    unusable for the one project that legitimately contains it.
    """
    from core.paths import DATA_DIR
    from core.sandbox.providers.seatbelt import sensitive_reads

    blocked = sensitive_reads(str(Path(DATA_DIR).resolve().parent))

    assert str(Path(DATA_DIR).resolve()) not in blocked


@requires_os
def test_a_read_only_profile_cannot_write_into_the_workspace(zone):
    """'read_only' means the project itself is protected from the child."""
    workspace, _home, _env = zone
    result = _run(zone, "open('mutated.txt', 'w').write('x')",
                  profile=profiles.READ_ONLY)

    assert result.ok is False
    assert not (workspace / "mutated.txt").exists()


# ── network ─────────────────────────────────────────────────────────────────


@requires_os
def test_a_no_network_profile_cannot_open_a_socket(zone):
    """The denial happens at the syscall, not by inspecting the source."""
    code = ("import socket\n"
            "s = socket.socket()\n"
            "s.settimeout(2)\n"
            "s.connect(('127.0.0.1', 1))\n"
            "print('CONNECTED')\n")
    result = _run(zone, code)

    assert result.ok is False
    assert "CONNECTED" not in result.stdout
    assert "Permission" in result.stderr or "not permitted" in result.stderr.lower()


def test_a_long_wait_does_not_block_another_dialog():
    """A running child must not freeze the tool dispatches of other dialogs.

    The binding lock serializes the config swap of one dispatch. A long
    ``run_code`` therefore used to hold it for its whole runtime, and a
    second dialog waited minutes for its first tool call. The wait itself
    is what gets suspended, so the guarantee under test is the ORDER: the
    other dispatch finishes while the first child is still running.
    """
    from dev_agent import workspace_binding as wb

    first_done = threading.Event()
    second_entered = threading.Event()
    order = []

    def long_run():
        with wb.thread_context("dialog-one"):
            with wb.without_binding_lock() as released:
                order.append(("released", released))
                second_entered.set()
                time.sleep(1.5)
        first_done.set()

    def other_dialog():
        second_entered.wait(timeout=10)
        started = time.monotonic()
        with wb.thread_context("dialog-two"):
            order.append(("second", round(time.monotonic() - started, 2)))

    wb.register_thread("dialog-one", workspace=str(Path.home()))
    wb.register_thread("dialog-two", workspace=str(Path.home()))
    one = threading.Thread(target=long_run, daemon=True)
    two = threading.Thread(target=other_dialog, daemon=True)
    one.start()
    two.start()
    two.join(timeout=20)
    one.join(timeout=20)

    assert first_done.is_set()
    assert order[0] == ("released", True)
    # The other dialog entered while the first one was still waiting.
    assert order[1][0] == "second"
    assert order[1][1] < 1.0, "the second dispatch waited for the first child"


def test_the_binding_lock_is_balanced_after_a_suspended_wait():
    """Suspend/resume must leave the lock exactly as it was found."""
    from dev_agent import workspace_binding as wb

    wb.register_thread("dialog-balance", workspace=str(Path.home()))
    with wb.thread_context("dialog-balance"):
        with wb.without_binding_lock():
            # While suspended the lock is free: another thread can take it.
            acquired = wb.sync_lock().acquire(timeout=1)
            if acquired:
                wb.sync_lock().release()
        assert acquired is True
    # After the block the lock is free again: a deadlock would hang the suite.
    assert wb.sync_lock().acquire(timeout=1) is True
    wb.sync_lock().release()


# ── resource ceilings ───────────────────────────────────────────────────────


def test_the_launcher_plans_only_limits_the_platform_can_hold():
    """A plan is per platform: the same profile wraps differently."""
    limits = ResourceLimits(cpu_seconds=7, memory_mb=512, max_file_bytes=4096,
                            max_processes=8)

    linux = launcher.limits_spec(limits, platform="linux")
    macos = launcher.limits_spec(limits, platform="darwin")
    windows = launcher.limits_spec(limits, platform="win32")

    assert linux == {"RLIMIT_CPU": 7, "RLIMIT_FSIZE": 4096,
                     "RLIMIT_AS": 512 * 1024 * 1024}
    assert "RLIMIT_AS" not in macos, "macOS refuses a ceiling below the mapped size"
    assert "RLIMIT_NPROC" not in linux, "the per-user process count is not a per-run bound"
    assert windows == {}, "Windows has no rlimit mechanism to wrap"


def test_the_launcher_is_skipped_when_there_is_nothing_to_enforce():
    """No ceiling means no extra interpreter in front of the program."""
    argv = [sys.executable, "-c", "print(1)"]
    empty = ResourceLimits(cpu_seconds=0, max_processes=0, max_file_bytes=0,
                           memory_mb=0)

    assert launcher.limits_spec(empty) == {}
    assert launcher.wrap(argv, empty) == argv


def test_the_launcher_bounds_the_size_of_a_written_file(tmp_path):
    """The disk ceiling of the profile reaches the kernel."""
    target = tmp_path / "big.bin"
    code = "open(%r, 'wb').write(b'x' * (4 * 1024 * 1024))" % str(target)
    argv = launcher.wrap([sys.executable, "-c", code],
                         ResourceLimits(max_file_bytes=1024 * 1024))

    completed = subprocess.run(argv, capture_output=True, text=True, timeout=120)

    assert completed.returncode != 0, "the file ceiling was not enforced"
    assert not target.exists() or target.stat().st_size <= 1024 * 1024


def test_the_launcher_bounds_cpu_time():
    """A busy loop is stopped by the CPU ceiling, not by a wall clock."""
    argv = launcher.wrap([sys.executable, "-c", "while True: pass"],
                         ResourceLimits(cpu_seconds=1))

    completed = subprocess.run(argv, capture_output=True, text=True, timeout=120)

    assert completed.returncode != 0
    # -SIGXCPU (or the shell-style 128+24) is the kernel telling the child
    # that it used up its CPU budget.
    assert completed.returncode in (-24, 152), completed.returncode


# ── cancellation ────────────────────────────────────────────────────────────


def test_a_running_child_is_stopped_from_another_thread(zone):
    """Stop reaches the process, not just the loop's next step boundary.

    The child is a real process sleeping far longer than the test: only a
    kill can end it. That is what makes the Stop button honest for
    ``run_code``, whose whole point is that the platform is not control of
    the program it started.
    """
    _workspace, _home, _env = zone
    outcome = {}
    started = threading.Event()

    def worker():
        code = ("import time\n"
                "print('STARTED', flush=True)\n"
                "time.sleep(120)\n")
        outcome["result"] = _run(zone, code, profile=profiles.LEGACY_PROCESS,
                                 thread_id="cancel-thread")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    for _ in range(200):
        if active.active_runs():
            started.set()
            break
        time.sleep(0.05)
    assert started.is_set(), "the run was never registered"

    stopped = active.cancel_thread("cancel-thread")
    thread.join(timeout=60)

    assert stopped >= 1
    assert not thread.is_alive(), "the child did not die"
    result = outcome["result"]
    assert result.cancelled is True
    assert result.ok is False


def test_cancelling_a_run_kills_its_grandchildren(zone):
    """Stop reaches the whole process group, not just the direct child.

    A snippet that spawns a helper leaves an ORPHAN when only the direct
    child is killed: the helper keeps the write handle open and keeps
    running after the user pressed Stop. The kill therefore targets the
    process group, and this test is what proves it.
    """
    workspace, _home, _env = zone
    marker = workspace / "orphan-alive.txt"
    outcome = {}

    def worker():
        code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "\"import time, pathlib, sys; time.sleep(30); "
            "pathlib.Path(sys.argv[1]).write_text('alive')\", "
            "%r])\n"
            "print('SPAWNED', flush=True)\n"
            "time.sleep(30)\n" % str(marker)
        )
        outcome["result"] = _run(zone, code, profile=profiles.LEGACY_PROCESS,
                                 thread_id="orphan-thread")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    for _ in range(200):
        if active.active_runs():
            break
        time.sleep(0.05)

    assert active.cancel_thread("orphan-thread") == 1
    thread.join(timeout=60)

    # Give a surviving grandchild the time it needed to write its marker.
    time.sleep(2.5)
    assert outcome["result"].cancelled is True
    assert not marker.exists(), "a grandchild survived the cancellation"


def test_cancelling_one_run_leaves_its_sibling_alive(zone):
    """A per-run Stop must not take down another run of the same dialog."""
    _workspace, _home, _env = zone
    outcome = {}

    def worker(name: str):
        outcome[name] = _run(zone, "import time; time.sleep(3); print('DONE')",
                             profile=profiles.LEGACY_PROCESS,
                             thread_id="shared-thread")

    first = threading.Thread(target=worker, args=("first",), daemon=True)
    second = threading.Thread(target=worker, args=("second",), daemon=True)
    first.start()
    second.start()
    for _ in range(200):
        if len(active.active_runs()) >= 2:
            break
        time.sleep(0.05)

    keys = sorted(run["key"] for run in active.active_runs())
    assert len(keys) == 2, "both runs must be registered"
    assert active.cancel_run(keys[0]) is True

    first.join(timeout=60)
    second.join(timeout=60)

    stopped = [name for name in ("first", "second") if outcome[name].cancelled]
    assert len(stopped) == 1, "exactly one run was stopped"


def test_cancelling_another_dialog_leaves_this_run_alone(zone):
    """Stop is scoped to the dialog that pressed it."""
    _workspace, _home, _env = zone
    outcome = {}

    def worker():
        outcome["result"] = _run(zone, "import time; time.sleep(2); print('DONE')",
                                 profile=profiles.LEGACY_PROCESS,
                                 thread_id="untouched-thread")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    for _ in range(200):
        if active.active_runs():
            break
        time.sleep(0.05)

    assert active.cancel_thread("some-other-thread") == 0
    thread.join(timeout=60)

    assert outcome["result"].cancelled is False
    assert outcome["result"].ok is True

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
