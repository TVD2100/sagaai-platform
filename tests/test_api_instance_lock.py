"""Tests of the single-instance lock.

The lock is the only thing standing between the platform and two server
processes that each believe they own the run registry. The tests therefore
check the properties that make it a lock rather than a file: a second
acquisition fails, a released lock can be taken again, and the metadata of
the holder is readable by the process that was refused.

A real second PROCESS is used where it matters. ``flock`` is per descriptor,
so two objects in one process would not prove anything about two servers.
"""

import os
import subprocess
import sys
import textwrap

import pytest

from server.instance_lock import EXIT_ALREADY_RUNNING, InstanceLock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─── In-process properties ─────────────────────────────────────────────────


def test_a_free_lock_is_acquired(tmp_path):
    lock = InstanceLock(path=tmp_path / "server.lock", port=8765)
    assert lock.acquire() is True
    try:
        assert lock.path.exists()
    finally:
        lock.release()


def test_a_released_lock_can_be_acquired_again(tmp_path):
    path = tmp_path / "server.lock"
    first = InstanceLock(path=path)
    assert first.acquire() is True
    first.release()
    second = InstanceLock(path=path)
    assert second.acquire() is True
    second.release()


def test_the_holder_metadata_names_the_process(tmp_path):
    path = tmp_path / "server.lock"
    lock = InstanceLock(path=path, port=9999, version="1.2.3")
    assert lock.acquire() is True
    lock.release()
    import json
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["port"] == 9999
    assert payload["version"] == "1.2.3"


def test_the_context_manager_raises_when_the_lock_is_taken(monkeypatch, tmp_path):
    path = tmp_path / "server.lock"
    holder = InstanceLock(path=path)
    assert holder.acquire() is True
    try:
        blocked = InstanceLock(path=path)
        monkeypatch.setattr(blocked, "acquire", lambda: False)
        with pytest.raises(RuntimeError):
            with blocked:
                pass  # pragma: no cover - never reached
    finally:
        holder.release()


def test_release_is_idempotent(tmp_path):
    lock = InstanceLock(path=tmp_path / "server.lock")
    lock.acquire()
    lock.release()
    lock.release()  # must not raise


# ─── Two real processes ────────────────────────────────────────────────────


def _holder_script(path):
    return textwrap.dedent(
        """
        import sys, time
        sys.path.insert(0, %r)
        from server.instance_lock import InstanceLock
        lock = InstanceLock(path=%r, port=8765, version='test')
        if not lock.acquire():
            print('REFUSED')
            sys.exit(1)
        print('HELD', flush=True)
        time.sleep(30)
        """
    ) % (ROOT, str(path))


def test_a_second_process_is_refused_and_reads_the_holder(tmp_path):
    path = tmp_path / "server.lock"
    holder = subprocess.Popen([sys.executable, "-c", _holder_script(path)],
                              stdout=subprocess.PIPE, text=True)
    try:
        line = holder.stdout.readline().strip()
        assert line == "HELD"
        contender = InstanceLock(path=path)
        assert contender.acquire() is False
        assert contender.holder.get("pid") == holder.pid
        assert contender.holder.get("version") == "test"
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_the_lock_is_free_after_the_holder_dies(tmp_path):
    path = tmp_path / "server.lock"
    holder = subprocess.Popen([sys.executable, "-c", _holder_script(path)],
                              stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "HELD"
    holder.kill()
    holder.wait(timeout=10)
    lock = InstanceLock(path=path)
    assert lock.acquire() is True
    lock.release()


def test_the_refusal_exit_code_is_distinct_from_a_crash():
    assert EXIT_ALREADY_RUNNING == 3
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
