# -*- coding: utf-8 -*-
"""tests/test_orchestration_tools_facade.py - the facade contract.

``core.orchestration.tools`` was split into ``tools_*`` modules by topic. The
split is only safe while three properties hold, and each one can break
silently:

1. every name the module used to expose is still reachable at the OLD path -
   the agent loop and the catalog import from there;
2. the facade stays the place where the names are BOUND - a test replaces
   ``tools.spawn_agent`` and ``tools.time``, and a local binding in an
   implementation module would freeze the original and make the replacement
   invisible;
3. nothing in the implementation layer imports the facade at module level -
   that would close an import cycle.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.orchestration.tools as T  # noqa: E402

PACKAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "core", "orchestration")

#: Every name the module exposed BEFORE the split. The list is the contract:
#: removing a name is a breaking change for the loop and for the catalog.
PUBLIC_NAMES = (
    # constants
    "MAX_STATUS_WAIT_SECONDS", "MAX_STATE_CHARS", "MAX_SLEEP_SECONDS",
    # notes
    "run_note", "_attach_note", "fence_question_payload", "_note_for_payload", "_journal_note",
    # rights
    "_allowlist_context", "_available_agents", "check_agent_allowed",
    "check_assistant_allowed", "_validate_brief",
    # records
    "_load_run_record", "_resolve_run_root", "_confine_run", "_is_live_result",
    "_child_thread_row_missing",
    # tools
    "spawn_agent", "invoke_assistant", "subagent_status", "subagent_result",
    "read_subagent_state", "read_subagent_thread", "cancel_subagent", "resume_subagent",
    "verify_result", "sleep_tool", "ask_parent", "answer_child", "read_dialog_limits",
    # wait helpers
    "_terminal_run_ids", "_active_run_ids",
    # catalog
    "ask_parent_tool_entry", "answer_child_tool_entry", "spawn_tool_entry",
    "subagent_status_tool_entry", "read_subagent_state_tool_entry", "resume_subagent_tool_entry",
    "read_subagent_thread_tool_entry", "cancel_subagent_tool_entry", "subagent_result_tool_entry",
    "sleep_tool_entry", "verify_result_tool_entry", "invoke_tool_entry",
)


def _facade_tree():
    with open(T.__file__, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _module_source(name):
    path = os.path.join(PACKAGE_DIR, name + ".py")
    if not os.path.isfile(path):
        return None, None
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    return source, ast.parse(source)


# ─── the public path survives ────────────────────────────────────────────────


def test_every_name_the_module_exposed_is_still_reachable():
    """A missing name breaks the loop or the catalog at import time."""
    missing = [name for name in PUBLIC_NAMES if not hasattr(T, name)]
    assert missing == [], "names lost in the split: %s" % missing


def test_the_facade_defines_no_functions_of_its_own():
    """Every tool lives in a topic module; the facade only binds names."""
    defined = [n.name for n in _facade_tree().body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert defined == [], "the facade must only re-export, found: %s" % defined


def test_the_facade_stays_small():
    """A facade that grows back into a monolith defeats the split."""
    with open(T.__file__, encoding="utf-8") as handle:
        assert len(handle.read().splitlines()) < 220


def test_the_facade_lists_every_public_name_in_all():
    """``__all__`` is the executable copy of the contract above."""
    declared = set(getattr(T, "__all__", []) or [])
    assert set(PUBLIC_NAMES) <= declared


# ─── the facade owns the bindings ────────────────────────────────────────────


def test_the_facade_owns_the_clock():
    """``monkeypatch.setattr(tools, "time", ...)`` must steer the wait.

    The wait tool had a module-level ``import time`` before the split. A moved
    function that kept that import would sleep in real time while the test
    believes it controls the clock - so the sleep below is observed through a
    fake clock installed on the FACADE.
    """

    class FakeTime:
        def __init__(self):
            self.now = 1000.0
            self.slept = []

        def monotonic(self):
            return self.now

        def time(self):
            return self.now

        def sleep(self, seconds):
            self.slept.append(seconds)
            self.now += seconds

    class Service:
        def get_record(self, run_id):
            return None

        def is_cancelled(self, run_id):
            return False

        def has_notifications(self, root):
            return False

    fake = FakeTime()
    original = T.time
    T.time = fake
    try:
        out = T.sleep_tool(seconds=0.05, until="timeout", service=Service())
    finally:
        T.time = original

    assert out["ok"] is True
    assert out["woken_by"] == "timeout"
    assert fake.slept, "the wait slept on a clock the facade does not own"


def test_resume_reaches_the_facade_binding_of_spawn_agent():
    """A replaced ``tools.spawn_agent`` must be the one resume calls.

    The check is structural on purpose: it reads the implementation module and
    asserts the call goes through the facade ATTRIBUTE, which is what keeps a
    ``monkeypatch.setattr(tools, "spawn_agent", ...)`` in charge.
    """
    source, tree = _module_source("tools_control")
    assert tree is not None, "tools_control.py is missing"

    imported = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "core.orchestration.tools_spawn":
            imported |= {alias.name for alias in node.names}
    assert "spawn_agent" not in imported, (
        "a module-level import of spawn_agent freezes the original function: "
        "the facade replacement would never reach resume_subagent"
    )

    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "spawn_agent" in attributes, "resume_subagent must call facade.spawn_agent"


# ─── the import graph stays acyclic ──────────────────────────────────────────


def test_no_implementation_module_imports_the_facade_at_module_level():
    """An implementation -> facade import at import time closes a cycle."""
    offenders = []
    for name in os.listdir(PACKAGE_DIR):
        if not (name.startswith("tools_") and name.endswith(".py")):
            continue
        source, tree = _module_source(name[:-3])
        for node in tree.body:  # module level only: a lazy import inside a
            # function is exactly how the clock reaches the facade safely.
            if isinstance(node, ast.ImportFrom) and node.module == "core.orchestration.tools":
                offenders.append(name)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "core.orchestration.tools":
                        offenders.append(name)
    assert offenders == [], "module-level imports of the facade: %s" % offenders


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
