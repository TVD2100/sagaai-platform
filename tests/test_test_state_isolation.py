# -*- coding: utf-8 -*-
"""tests/test_test_state_isolation.py - the suite owns the global state it mutates.

Several code paths mutate process-global state: ``dev_agent.config`` holds the
active workspace root and the ``WORKSPACE_SELECTED`` flag, and the workspace
registry holds the per-thread bindings. Rendering an orchestrator page,
resetting a dialog or opening the empty state all switch that state.

A leaked ``WORKSPACE_SELECTED=False`` blocks file tools in every later test of
the same pytest run, so the leak surfaces far from its cause. The rule this
module guards:

  - ``tests/conftest.py`` ships ``_restore_dev_agent_state`` but does NOT make
    it autouse - an autouse hook hides which tests depend on it and charges
    every test in the suite for a snapshot it never needs;
  - a test module that mutates the global state either requests that fixture
    by name, restores the state itself, or lives under ``tests/scenarios/``
    where the folder-level conftest owns the restore.
"""
import ast
import os

TESTS_ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_ROOT)
BASE_CONFTEST = os.path.join(TESTS_ROOT, "conftest.py")
SCENARIO_DIR = os.path.join(TESTS_ROOT, "scenarios")

RESTORE_FIXTURE = "_restore_dev_agent_state"

#: Calls that can leave ``WORKSPACE_SELECTED=False`` behind - the flag that
#: blocks file tools in every later test. ``set_workspace`` is not listed: it
#: always selects a folder, so it cannot trip the empty-state guard.
MUTATING_CALLS = frozenset({"apply_paths", "_reset_dialog"})


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _fixture_defs(tree):
    """Yield (name, is_autouse_requested) for every fixture defined in a module."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            name = getattr(target, "attr", None) or getattr(target, "id", None)
            if name != "fixture":
                continue
            autouse = False
            if isinstance(deco, ast.Call):
                for kw in deco.keywords:
                    if kw.arg == "autouse" and isinstance(kw.value, ast.Constant):
                        autouse = bool(kw.value.value)
            yield node.name, autouse


def _calls_mutating_state(tree):
    """Return the mutating call names the module makes anywhere in its code."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in MUTATING_CALLS:
                found.add(name)
    return found


def _restores_itself(source, tree):
    """True when the module snapshots and restores the config state itself."""
    has_snapshot = "snapshot_state" in source
    has_restore = "restore_state" in source
    if has_snapshot and has_restore:
        return True
    # A module-level pytestmark that requests the shared fixture counts too.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", None) == "pytestmark" and RESTORE_FIXTURE in ast.dump(node.value):
                    return True
    return False


def _requests_fixture(tree):
    """True when a test function or fixture takes the restore fixture by name."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        names = [a.arg for a in list(args.args) + list(args.kwonlyargs)]
        if RESTORE_FIXTURE in names:
            return True
    return False


def _iter_test_modules():
    for root, dirs, files in os.walk(TESTS_ROOT):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith(".py") and name != "conftest.py":
                yield os.path.join(root, name)


def test_base_conftest_does_not_autouse_the_restore_fixture():
    """The restore fixture is opt-in; an autouse hook would hide its consumers."""
    tree = ast.parse(_read(BASE_CONFTEST))
    fixtures = dict(_fixture_defs(tree))
    assert RESTORE_FIXTURE in fixtures, "tests/conftest.py no longer defines the restore fixture"
    assert fixtures[RESTORE_FIXTURE] is False, (
        "_restore_dev_agent_state is autouse again: it makes every test in the "
        "suite snapshot state it never touches and hides which tests really "
        "depend on it"
    )


def test_scenario_folder_owns_its_restore():
    """The scenario suite drives real pages, so its folder-level hook restores."""
    path = os.path.join(SCENARIO_DIR, "conftest.py")
    assert os.path.exists(path), "tests/scenarios/conftest.py is missing"
    tree = ast.parse(_read(path))
    autouse_hooks = [name for name, autouse in _fixture_defs(tree) if autouse]
    assert autouse_hooks, "tests/scenarios/conftest.py no longer restores state after a scenario"
    source = _read(path)
    assert RESTORE_FIXTURE in source, (
        "the scenario autouse hook must delegate to _restore_dev_agent_state, "
        "otherwise it snapshots nothing"
    )


def test_every_module_that_mutates_global_state_restores_it():
    """A module that switches the workspace state must hand it back afterwards."""
    offenders = []
    checked = 0
    for path in _iter_test_modules():
        source = _read(path)
        tree = ast.parse(source)
        calls = _calls_mutating_state(tree)
        if not calls:
            continue
        checked += 1
        if path.startswith(SCENARIO_DIR + os.sep):
            continue
        if _requests_fixture(tree) or _restores_itself(source, tree):
            continue
        rel = os.path.relpath(path, REPO_ROOT)
        offenders.append("%s calls %s" % (rel, ", ".join(sorted(calls))))
    assert checked, "no module mutating the global workspace state was found - the scan is broken"
    assert not offenders, (
        "these modules switch the process-global workspace state without restoring it; "
        "request the _restore_dev_agent_state fixture or snapshot/restore it yourself:\n  "
        + "\n  ".join(offenders)
    )


# ─── Runtime-data isolation ────────────────────────────────────────────────
#
# A test that creates a connection, an employee or an assistant writes through
# ``core.paths.DATA_DIR``: ``core.connectors._connectors_root`` and its peers
# read that attribute at call time. Without an override the write lands in the
# real installation and the entity outlives the run, so the user sees test
# connections on the Connectors page and test employees in the sidebar.
#
# The rule this guard enforces: a module that calls one of the writing APIs
# must also isolate DATA_DIR - by touching ``DATA_DIR`` itself, by importing
# the isolation fixture from a helper module, or by requesting a fixture whose
# name ends with ``data_dir``.

#: Calls that persist an entity under ``core.paths.DATA_DIR``.
WRITER_CALLS = frozenset({
    "create_connection", "update_connection", "delete_connection",
    "set_connection_memory", "instantiate_preset",
    "create_orchestrator", "update_orchestrator", "delete_orchestrator",
    "save_orchestrator_function", "save_orchestrator_instruction",
    "create_assistant", "update_assistant_by_id",
})

#: A fixture whose name ends like this points DATA_DIR at a temporary folder.
ISOLATION_FIXTURE_SUFFIX = "data_dir"



def _called_names(tree, names):
    """Return the subset of *names* the module calls anywhere in its code."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in names:
                found.add(name)
    return found


def _imports_isolation_fixture(tree):
    """True when the module imports an isolation fixture from another module.

    Re-exporting ``isolated_data_dir`` from a helper module is the recommended
    way to share it, so an import counts exactly like a local definition.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name.endswith(ISOLATION_FIXTURE_SUFFIX):
                    return True
    return False


def _requests_isolation_fixture(tree):
    """True when any function asks for a fixture named ``*data_dir``."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        for arg in list(args.args) + list(args.kwonlyargs):
            if arg.arg.endswith(ISOLATION_FIXTURE_SUFFIX):
                return True
    return False


def _isolates_data_dir(source, tree):
    """True when the module keeps its writes out of the real DATA_DIR."""
    if "DATA_DIR" in source:
        return True
    return _imports_isolation_fixture(tree) or _requests_isolation_fixture(tree)


def test_every_module_that_writes_runtime_data_isolates_data_dir():
    """A module that persists an entity must not write it into the install."""
    offenders = []
    checked = 0
    for path in _iter_test_modules():
        source = _read(path)
        tree = ast.parse(source)
        calls = _called_names(tree, WRITER_CALLS)
        if not calls:
            continue
        checked += 1
        if _isolates_data_dir(source, tree):
            continue
        rel = os.path.relpath(path, REPO_ROOT)
        offenders.append("%s calls %s" % (rel, ", ".join(sorted(calls))))
    assert checked, "no module writing runtime data was found - the scan is broken"
    assert not offenders, (
        "these modules write runtime data through core.paths.DATA_DIR without "
        "isolating it; request an ``*data_dir`` fixture or point DATA_DIR at "
        "tmp_path yourself:\n  " + "\n  ".join(offenders)
    )


#: A module that writes a connection without isolating DATA_DIR - the shape
#: this guard exists to catch.
_LEAKY_SOURCE = (
    "def test_makes_a_connection(preset_root):\n"
    "    instantiate_preset('demo', 'mcp', preset_root=preset_root)\n"
)

#: The same write after the fix: the isolation fixture is requested by name.
_FIXED_SOURCE = (
    "def test_makes_a_connection(preset_root, isolated_data_dir):\n"
    "    instantiate_preset('demo', 'mcp', preset_root=preset_root)\n"
)


def test_guard_flags_a_writer_without_isolation():
    """The guard must see the leak it was written for."""
    tree = ast.parse(_LEAKY_SOURCE)
    assert _called_names(tree, WRITER_CALLS) == {"instantiate_preset"}
    assert _isolates_data_dir(_LEAKY_SOURCE, tree) is False


def test_guard_accepts_every_isolation_form():
    """A requested, an imported and an explicit isolation all count."""
    assert _isolates_data_dir(_FIXED_SOURCE, ast.parse(_FIXED_SOURCE)) is True
    imported = "from tests.test_ssh_connector import isolated_data_dir\n"
    assert _isolates_data_dir(imported, ast.parse(imported)) is True
    explicit = "import core.paths\ncore.paths.DATA_DIR = '/tmp/x'\n"
    assert _isolates_data_dir(explicit, ast.parse(explicit)) is True