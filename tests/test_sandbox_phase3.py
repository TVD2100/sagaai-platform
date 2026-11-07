# -*- coding: utf-8 -*-
"""tests/test_sandbox_phase3.py - containers, WSL and transport approval.

What is covered here:

- the container provider translates a HOST command for the image instead of
  hoping the paths exist there, and mounts what the command names;
- a container run passes the ceilings the runtime enforces (process count,
  memory) - the reason a container is stronger than a namespace alone;
- a cancelled container run stops the CONTAINER, not just the client proxy;
- the WSL provider translates Windows paths and refuses a path its
  distribution cannot reach rather than running a different file;
- WSL claims ``network-off`` only when ``unshare -n`` actually worked;
- an approval ticket is bound to one operation, is single-use, expires, and
  cannot be borrowed by another run;
- the HTTP confirmation route refuses a run that is not parked and a body
  whose decision is not a decision.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.sandbox.contracts import ResourceLimits, SandboxProfile, SandboxRequest
from core.sandbox.providers import container as container_mod
from core.sandbox.providers import wsl as wsl_mod

LIMITS = ResourceLimits(timeout_s=30, memory_mb=256, max_processes=8)


def _profile(**overrides) -> SandboxProfile:
    """Return a container profile, with the given fields replaced."""
    base = dict(name="workspace_write", isolation="container", network=False,
                writable=True, requires=("network-off", "write-confined"),
                limits=LIMITS)
    base.update(overrides)
    return SandboxProfile(**base)


def _request(tmp_path, outside_script: bool = False) -> SandboxRequest:
    """Build a request whose command names a real host script.

    ``outside_script`` puts the script outside the workspace, which is the
    case that forces a separate read-only mount.
    """
    workspace = tmp_path / "project"
    workspace.mkdir(exist_ok=True)
    script_dir = tmp_path if outside_script else workspace
    script = script_dir / "script.py"
    script.write_text("print(1)", encoding="utf-8")
    return SandboxRequest(
        tool="run_code",
        argv=(sys.executable, str(script), "--flag"),
        cwd=str(workspace), env={"PATH": "/usr/bin"},
        workspace=str(workspace), profile="workspace_write",
    )


# ── container provider ──────────────────────────────────────────────────────


def test_the_host_interpreter_becomes_python3_inside_the_image():
    """The image has no host interpreter; the command must be translated."""
    assert container_mod.translate_argv(["/usr/bin/python3.12", "x.py"]) == ["python3", "x.py"]
    assert container_mod.translate_argv(["node", "x.js"]) == ["node", "x.js"]


def test_a_script_outside_the_workspace_is_mounted_read_only(tmp_path):
    """The analysed file must exist inside, and must not become writable."""
    request = _request(tmp_path, outside_script=True)
    mounts = container_mod._mounts(request, _profile())
    script = request.argv[1]
    matching = [item for item in mounts if item[0] == script]
    assert matching, "the script the command names must be mounted"
    assert matching[0][2] is False


def test_a_script_inside_the_workspace_is_not_mounted_twice(tmp_path):
    """One mount per path: a duplicate makes the runtime refuse the run."""
    request = _request(tmp_path)
    mounts = container_mod._mounts(request, _profile())
    inside = [item for item in mounts if item[0] == request.argv[1]]
    assert inside == []
    assert [item[0] for item in mounts] == [str(tmp_path / "project")]


def test_the_workspace_follows_the_profile(tmp_path):
    """A read-only profile must keep the project read-only."""
    request = _request(tmp_path)
    workspace = str(tmp_path / "project")
    read_only = container_mod._mounts(request, _profile(writable=False))
    assert [i for i in read_only if i[0] == workspace][0][2] is False
    writable = container_mod._mounts(request, _profile(writable=True))
    assert [i for i in writable if i[0] == workspace][0][2] is True


def test_the_runtime_enforces_the_process_and_memory_ceilings(tmp_path):
    """These ceilings are what a namespace alone cannot offer."""
    request = _request(tmp_path)
    argv = container_mod.build_argv("docker", "name", "python:3.12-slim",
                                    _profile(), request, LIMITS)
    assert argv[argv.index("--pids-limit") + 1] == "8"
    assert argv[argv.index("--memory") + 1] == "256m"
    assert "--read-only" in argv
    assert "--cap-drop" in argv and "ALL" in argv


def test_the_network_follows_the_profile(tmp_path):
    """No grant means no namespace, a grant means the default one."""
    request = _request(tmp_path)
    closed = container_mod.build_argv("docker", "n", "img", _profile(),
                                      request, LIMITS)
    assert closed[closed.index("--network") + 1] == "none"
    open_ = container_mod.build_argv("docker", "n", "img", _profile(network=True),
                                     request, LIMITS)
    assert open_[open_.index("--network") + 1] == "bridge"


def test_the_provider_claims_only_what_it_enforces():
    """A capability is a promise the policy relies on."""
    provider = container_mod.ContainerProvider()
    assert provider.isolation == "container"
    assert set(provider.capabilities) == {"network-off", "write-confined",
                                          "process-limit"}


def test_a_missing_docker_is_reported_not_guessed(monkeypatch):
    """Availability is a probe result, never an assumption."""
    monkeypatch.setattr(container_mod, "_resolve_docker", lambda: None)
    assert container_mod.ContainerProvider().available() is False


def test_a_cancelled_run_stops_the_container_not_the_client(monkeypatch):
    """The client is a proxy; the container belongs to the daemon."""
    calls = []

    class _FakeSub:
        returncode = 0

        @staticmethod
        def run(argv, **kwargs):
            calls.append(list(argv))
            return _FakeSub()

    monkeypatch.setattr(container_mod, "subprocess", _FakeSub)
    container_mod._kill_container("/usr/bin/docker", "sagaai-sbx-1")
    assert calls == [["/usr/bin/docker", "kill", "sagaai-sbx-1"]]


# ── WSL provider ────────────────────────────────────────────────────────────


def test_a_windows_path_is_translated_for_the_distribution():
    """The distribution sees drives under /mnt."""
    assert wsl_mod.to_wsl_path("C:\\work\\a.py") == "/mnt/c/work/a.py"
    assert wsl_mod.to_wsl_path("d:/work/b.py") == "/mnt/d/work/b.py"
    assert wsl_mod.to_wsl_path("/usr/bin/python3") == "/usr/bin/python3"


def test_the_command_is_translated_for_the_distribution():
    """Only the interpreter and the paths change; flags stay as they are."""
    argv = wsl_mod.translate_argv(["C:\\Python\\python.exe", "-m", "pytest",
                                    "C:\\proj\\t.py"])
    assert argv == ["python3", "-m", "pytest", "/mnt/c/proj/t.py"]


def test_a_path_the_distribution_cannot_reach_is_untranslatable():
    """Running another file than the analysed one is worse than a refusal."""
    assert wsl_mod.untranslatable(["C:\\Py\\python.exe", "\\\\srv\\share\\x.py"])
    assert wsl_mod.untranslatable(["C:\\Py\\python.exe", "C:\\proj\\t.py"]) == ""


def test_the_run_that_cannot_be_translated_does_not_start(monkeypatch):
    """The refusal is a result, and nothing is executed."""
    monkeypatch.setattr(wsl_mod, "_resolve_wsl", lambda: "C:\\Windows\\wsl.exe")
    request = SandboxRequest(tool="run_code",
                             argv=("C:\\Py\\python.exe", "\\\\srv\\share\\x.py"),
                             cwd="C:\\work", env={}, workspace="C:\\work")
    profile = _profile(isolation="os", requires=("write-confined",))
    result = wsl_mod.WslProvider().run(request, profile, LIMITS)
    assert result.ok is False
    assert "cannot reach" in result.error


def test_wsl_claims_network_off_only_after_a_successful_probe(monkeypatch):
    """A distribution that forbids ``unshare -n`` must not advertise it."""
    monkeypatch.setattr(wsl_mod, "_resolve_wsl", lambda: "wsl")

    class _Ok:
        returncode = 0

        @staticmethod
        def run(argv, **kwargs):
            return _Ok()

    class _Bad:
        returncode = 1

        @staticmethod
        def run(argv, **kwargs):
            return _Bad()

    monkeypatch.setattr(wsl_mod, "subprocess", _Ok)
    assert "network-off" in wsl_mod.WslProvider().capabilities
    monkeypatch.setattr(wsl_mod, "subprocess", _Bad)
    refused = wsl_mod.WslProvider()
    assert "network-off" not in refused.capabilities
    assert "write-confined" in refused.capabilities


def test_the_network_flag_reaches_the_command_only_when_provable():
    """Without the proof the profile simply cannot be honoured here."""
    request = SandboxRequest(tool="run_code", argv=("python.exe", "x.py"),
                             cwd="C:\\work", env={}, workspace="C:\\work")
    profile = _profile(isolation="os", network=False)
    isolated = wsl_mod.build_argv("wsl", profile, request, isolate_network=True)
    assert "unshare" in isolated
    plain = wsl_mod.build_argv("wsl", profile, request, isolate_network=False)
    assert "unshare" not in plain


def test_the_launcher_options_come_before_the_command():
    """``--cd`` is the launcher's; after ``--exec`` every word is the program's."""
    request = SandboxRequest(tool="run_code", argv=("C:\\Py\\python.exe", "x.py"),
                             cwd="C:\\work", env={}, workspace="C:\\work")
    argv = wsl_mod.build_argv("wsl", _profile(isolation="os"), request, False)
    assert argv.index("--cd") < argv.index("--exec")
    assert argv[argv.index("--cd") + 1] == "/mnt/c/work"


# ── approval tickets ────────────────────────────────────────────────────────


def test_a_ticket_belongs_to_one_operation(tmp_path, monkeypatch):
    """The answer a human gave covers the call they were shown."""
    from core.gateway import approvals

    monkeypatch.setattr(approvals, "_store_dir", lambda: str(tmp_path))
    assert approvals.issue("run-1", "run_code", {"code": "print(1)"})["ok"] is True
    assert approvals.consume("run-1", "run_code", {"code": "print(2)"}) == ""
    assert approvals.consume("run-1", "run_code", {"code": "print(1)"}) == ""


def test_a_ticket_is_single_use(tmp_path, monkeypatch):
    """A second identical call needs a second decision."""
    from core.gateway import approvals

    monkeypatch.setattr(approvals, "_store_dir", lambda: str(tmp_path))
    approvals.issue("run-2", "run_code", {"code": "print(1)"})
    assert approvals.consume("run-2", "run_code", {"code": "print(1)"}) == "allow"
    assert approvals.consume("run-2", "run_code", {"code": "print(1)"}) == ""


def test_an_absent_argument_does_not_change_the_operation(tmp_path, monkeypatch):
    """The parked call and the dispatched call spell the same request."""
    from core.gateway import approvals

    monkeypatch.setattr(approvals, "_store_dir", lambda: str(tmp_path))
    approvals.issue("run-3", "run_code", {"code": "x", "path": None})
    assert approvals.consume("run-3", "run_code", {"code": "x"}) == "allow"


def test_an_expired_ticket_is_not_an_answer(tmp_path, monkeypatch):
    """A decision nobody used in time is no longer a decision."""
    from core.gateway import approvals

    monkeypatch.setattr(approvals, "_store_dir", lambda: str(tmp_path))
    approvals.issue("run-4", "run_code", {"code": "x"}, ttl_seconds=1)
    record = approvals.peek("run-4")
    assert record is not None
    record["expires_at"] = time.time() - 1
    (tmp_path / "run-4.json").write_text(json.dumps(record), encoding="utf-8")
    assert approvals.consume("run-4", "run_code", {"code": "x"}) == ""


def test_a_denial_is_recorded_as_a_denial(tmp_path, monkeypatch):
    """Deny is an answer, not the absence of one."""
    from core.gateway import approvals

    monkeypatch.setattr(approvals, "_store_dir", lambda: str(tmp_path))
    approvals.issue("run-5", "run_code", {"code": "x"}, decision=approvals.DENY)
    assert approvals.consume("run-5", "run_code", {"code": "x"}) == "deny"


def test_an_unknown_decision_is_refused(tmp_path, monkeypatch):
    """A typo must never be read as an approval."""
    from core.gateway import approvals

    monkeypatch.setattr(approvals, "_store_dir", lambda: str(tmp_path))
    refused = approvals.issue("run-6", "run_code", {}, decision="maybe")
    assert refused["ok"] is False
    assert not (tmp_path / "run-6.json").exists()


def test_a_ticket_of_another_run_is_not_borrowed(tmp_path, monkeypatch):
    """The scope keeps an answer inside the run that received it."""
    from core.gateway import approvals

    monkeypatch.setattr(approvals, "_store_dir", lambda: str(tmp_path))
    approvals.issue("run-7", "run_code", {"code": "x"})
    assert approvals.consume("run-8", "run_code", {"code": "x"}) == ""


# ── the ticket meets the dispatch ───────────────────────────────────────────


def test_a_ticket_is_read_only_inside_its_own_run_scope(tmp_path, monkeypatch):
    """The scope is what makes a ticket findable, and it is not an argument."""
    from core.gateway import approvals
    from dev_agent import sandbox_approval

    monkeypatch.setattr(approvals, "_store_dir", lambda: str(tmp_path))
    approvals.issue("run-scope", "run_code", {"code": "print(1)"})

    outside = sandbox_approval.consume_transport_approval("run_code", {"code": "print(1)"})
    assert outside == "", "no scope means no ticket is consulted"

    with sandbox_approval.approval_scope("run-scope"):
        inside = sandbox_approval.consume_transport_approval(
            "run_code", {"code": "print(1)", "path": None})
    assert inside == "allow"


def test_a_denial_reaches_the_call_as_a_denial(tmp_path, monkeypatch):
    """Deny must be distinguishable from "no answer yet"."""
    from core.gateway import approvals
    from dev_agent import sandbox_approval

    monkeypatch.setattr(approvals, "_store_dir", lambda: str(tmp_path))
    approvals.issue("run-deny", "run_code", {"code": "print(1)"},
                    decision=approvals.DENY)
    with sandbox_approval.approval_scope("run-deny"):
        decision = sandbox_approval.consume_transport_approval(
            "run_code", {"code": "print(1)"})
    assert decision == "deny"


def test_the_scope_does_not_leak_out_of_its_block(tmp_path, monkeypatch):
    """A value set for one dispatch must not survive it."""
    from dev_agent import sandbox_approval

    with sandbox_approval.approval_scope("run-a"):
        assert sandbox_approval.current_scope() == "run-a"
    assert sandbox_approval.current_scope() == ""


def test_an_approved_ticket_runs_the_operation_instead_of_parking(monkeypatch):
    """The second dispatch of an approved call executes it."""
    from core.gateway import approvals
    from dev_agent import sandbox_approval
    from dev_agent.tools import exec as exec_mod

    approvals_store = {}
    monkeypatch.setattr(exec_mod, "approvals_granted", lambda: False)
    monkeypatch.setattr(exec_mod, "consume_transport_approval",
                        sandbox_approval.consume_transport_approval)
    monkeypatch.setattr(approvals, "_store_dir", lambda: "/tmp/unused-phase3")
    monkeypatch.setattr(approvals, "consume",
                        lambda run, tool, args: approvals_store.pop(run, ""))
    approvals_store["run-exec"] = "allow"

    class _Tools(exec_mod.ExecTools):
        """An executor whose sandboxed run is recorded, not started."""

        def __init__(self):
            self.calls = []

        def _run_under_sandbox(self, tool, cmd, env, timeout_s, code_hash):
            self.calls.append((tool, list(cmd)))
            return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

        def deps(self):
            class _Deps:
                class config:
                    DEFAULT_ENCODING = "utf-8"
            return _Deps()

        _safety_enabled = True

    tools = _Tools()
    with sandbox_approval.approval_scope("run-exec"):
        result = tools._execute("run_code", code="print(1)", path=None,
                                timeout_s=10)
    assert result.get("ok") is True
    assert tools.calls and tools.calls[0][0] == "run_code"
    assert not result.get("confirmation_required")


# ── HTTP confirmation route ─────────────────────────────────────────────────


def test_a_run_that_is_not_parked_cannot_be_confirmed(monkeypatch):
    """The route refuses a decision that has nothing to decide."""
    from core.gateway import errors as gateway_errors
    from server.routes import runs as route_runs
    from server.schemas import ConfirmIn

    class _Snapshot:
        run_id = "r1"
        status = "running"
        pending_input = None

    monkeypatch.setattr(route_runs.deps, "run_id_path", lambda rid: rid)
    monkeypatch.setattr(route_runs.gateway_runs, "get_run", lambda rid: _Snapshot())
    with pytest.raises(gateway_errors.GatewayError) as caught:
        route_runs.confirm_run("r1", ConfirmIn(decision="allow"), scope=[])
    assert caught.value.code == "nothing_to_confirm"


def test_only_a_confirmable_pause_keeps_its_live_state(tmp_path, monkeypatch):
    """A pause with no way to resume it must not hold the slot forever."""
    from core.gateway import dialog as dialog_mod
    from core.gateway.dto import DialogStepResult, PendingInput

    released = []
    forgotten = []
    monkeypatch.setattr(dialog_mod.runner, "close_root", lambda *a, **k: None)

    class _Service:
        @staticmethod
        def save_record(run_id):
            return None

    monkeypatch.setattr(dialog_mod.runs, "get_service", lambda: _Service())
    monkeypatch.setattr(dialog_mod.runner, "release_root",
                        lambda service, rid: released.append(rid))
    monkeypatch.setattr(dialog_mod, "_unbind_workspace", lambda tid: None)
    monkeypatch.setattr(dialog_mod, "_forget", lambda rid: forgotten.append(rid))
    monkeypatch.setattr(dialog_mod, "_notify_closed", lambda rid, st: None)

    live = dialog_mod._LiveDialog()
    live.thread_id = "t1"
    for kind in ("confirmation", "sanitized", "question"):
        released.clear()
        forgotten.clear()
        result = DialogStepResult(run_id="run-" + kind, thread_id="t1",
                                  pending_input=PendingInput(kind=kind,
                                                             tool="run_code",
                                                             args={}))
        dialog_mod._close("run-" + kind, live, result)
        if kind == "confirmation":
            assert not released and not forgotten, kind
        else:
            assert released == ["run-" + kind], kind
            assert forgotten == ["run-" + kind], kind


def test_an_unknown_decision_word_is_refused(monkeypatch):
    """A bad body is a bad request, not a silent default."""
    from core.gateway import errors as gateway_errors
    from server.routes import runs as route_runs
    from server.schemas import ConfirmIn

    monkeypatch.setattr(route_runs.deps, "run_id_path", lambda rid: rid)
    with pytest.raises(gateway_errors.GatewayError) as caught:
        route_runs.confirm_run("r1", ConfirmIn(decision="yes-please"), scope=[])
    assert caught.value.code == "invalid_request"
