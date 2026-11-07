# -*- coding: utf-8 -*-
"""tests/test_orchestrator_api_facade.py - the facade contract of the API.

``core.orchestrators`` was split into a facade and ``core.orchestrator_*``
modules by topic. The split is only safe while four properties hold, and each
one can break silently:

1. every name the API exposed is still reachable at the OLD path - 47
   production modules and 71 test files import from there;
2. the facade keeps the bindings and holds no logic of its own, so a change
   cannot quietly turn it back into a monolith;
3. a replacement installed on the facade reaches the implementation - the
   tests patch ``orch_mod.get_orchestrator`` and
   ``"core.orchestrators._get_services"``, and a helper bound at import time
   would ignore both;
4. no satellite imports the facade at module level - that closes an import
   cycle, since the facade imports every satellite.

Every test imports its modules afresh through the isolation fixture: another
file may have reloaded the package, and a module object captured at import
time would then be a copy the satellites no longer resolve through.
"""
import ast
import importlib
import os

import pytest

CORE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")

#: Modules the API was split into. ``orchestrators.py`` itself is the facade.
SATELLITES = (
    "orchestrator_shared",
    "orchestrator_defaults",
    "orchestrator_store",
    "orchestrator_links",
    "orchestrator_prompt",
    "orchestrator_models",
    "orchestrator_transfer",
    "orchestrator_instr",
    "orchestrator_builtin",
)

#: Names the facade resolves late for its satellites: the tests replace them
#: through the module object or by the string path ``core.orchestrators.<name>``.
PATCHABLE = (
    "get_orchestrator",
    "get_allowed_agents",
    "get_web_search_prompt",
    "build_assistant_dicts",
    "get_allowed_assistants",
    "get_disabled_tools",
    "get_verifier_agent",
    "get_effective_agents",
    "list_orchestrators",
    "delete_orchestrator",
    "get_enabled_connections",
    "_sync_orchestrator_folder",
    "reset_builtin_prompt",
    "orch_get_instruction",
    "_get_services",
)


def _parse(path):
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _satellite_path(name):
    return os.path.join(CORE_DIR, name + ".py")


def _api():
    """Return the API module the satellites currently resolve through."""
    return importlib.import_module("core.orchestrators")


def _shared():
    """Return the late-binding layer of the current import tree."""
    return importlib.import_module("core.orchestrator_shared")


def _satellite(name):
    """Return one satellite of the current import tree."""
    return importlib.import_module("core." + name)


def _facade_functions(api):
    return [node for node in _parse(api.__file__).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


@pytest.fixture(autouse=True)
def _consistent_import_tree(isolated_app_modules):
    """Start every test from one self-consistent copy of the package.

    The API resolves patchable names through the facade object bound to its own
    module tree. A file that reloaded the package earlier leaves a second live
    copy in the process, and a test that mixes objects from both trees would
    check a facade nobody calls.
    """
    yield


# --- the public path survives ------------------------------------------------


def test_every_declared_name_is_reachable():
    """``__all__`` is the executable copy of the API contract."""
    api = _api()
    declared = list(getattr(api, "__all__", []) or [])
    assert declared, "the facade declares no __all__"
    missing = [name for name in declared if not hasattr(api, name)]
    assert missing == [], "names declared but missing: %s" % missing


def test_the_public_names_stay_importable():
    """A consumer imports these by name, not by ``getattr``."""
    api = _api()
    for name in ("DEVAGENT_SLUG", "UNIVERSAL_AGENT_SLUG", "build_assistant_dicts",
                 "build_skill_dicts", "get_orchestrator", "save_orchestrator",
                 "ensure_builtin_orchestrators", "load_devagent_config"):
        assert hasattr(api, name), name
    assert api.DEVAGENT_SLUG == "dev_agent"
    assert api.build_skill_dicts is api.build_assistant_dicts


# --- the facade stays a facade -----------------------------------------------


def test_the_facade_holds_only_wrappers():
    """Logic belongs to a satellite; the facade only binds and delegates."""
    offenders = []
    for node in _facade_functions(_api()):
        body = [stmt for stmt in node.body
                if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))]
        single_return = (len(body) == 1 and isinstance(body[0], ast.Return)
                         and isinstance(body[0].value, ast.Call))
        if not single_return:
            offenders.append(node.name)
    assert offenders == [], "the facade carries logic of its own: %s" % offenders


def test_every_wrapper_delegates_to_its_impl():
    """A wrapper must name the ``_impl`` function it stands for."""
    wrong = []
    for node in _facade_functions(_api()):
        returns = [stmt for stmt in ast.walk(node) if isinstance(stmt, ast.Return)]
        called = [stmt.value.func.id for stmt in returns
                  if isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Name)]
        if called != [node.name + "_impl"]:
            wrong.append((node.name, called))
    assert wrong == [], "wrappers that do not delegate to <name>_impl: %s" % wrong


def test_the_facade_stays_small():
    """A facade that grows back into a monolith defeats the split."""
    with open(_api().__file__, encoding="utf-8") as handle:
        assert len(handle.read().splitlines()) < 700


# --- a patch on the facade reaches the implementation ------------------------


def test_a_replaced_reader_reaches_the_satellites():
    """``orch_mod.get_orchestrator = ...`` must be what the satellites call."""
    api = _api()
    models = _satellite("orchestrator_models")

    original = api.get_orchestrator
    api.get_orchestrator = lambda slug: {"slug": slug, "config": {"web_search_prompt": "patched"}}
    try:
        assert models.get_web_search_prompt("probe") == "patched"
    finally:
        api.get_orchestrator = original


def test_a_replaced_services_reader_reaches_the_satellites():
    """``patch(\"core.orchestrators._get_services\")`` must steer the models."""
    api = _api()
    models = _satellite("orchestrator_models")

    original = api._get_services
    api._get_services = lambda: {"Probe": {"models": ["m"]}}
    try:
        assert models._get_services() == {"Probe": {"models": ["m"]}}
    finally:
        api._get_services = original


def test_the_shared_layer_resolves_the_current_api():
    """The layer must hand the satellites the API of their own tree."""
    api = _api()
    shared = _shared()

    assert shared.facade() is api
    assert getattr(shared.facade(), "__name__", "") == "core.orchestrators"


# --- satellites declare their patchable names late ---------------------------


def test_every_borrowed_patchable_name_is_declared_late():
    """A name a satellite BORROWS must be resolved late.

    A name the satellite defines itself needs no late binding: its own body is
    what a facade patch cannot reach anyway, and the facade calls the very same
    definition through its wrapper. What must never happen is a satellite using
    a patchable name that lives in ANOTHER module and binding it at import time.
    """
    offenders = []
    for name in SATELLITES:
        tree = _parse(_satellite_path(name))
        defined, late = set(), set()
        used = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                own = node.name[:-5] if node.name.endswith("_impl") else node.name
                defined.add(own)
                for child in ast.walk(node):
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                        used.add(child.id)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    if (isinstance(node.value, ast.Call)
                            and isinstance(node.value.func, ast.Name)
                            and node.value.func.id == "late"):
                        late.add(target.id)
                    else:
                        defined.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined.add(node.target.id)
        borrowed = (used & set(PATCHABLE)) - defined
        offenders.extend((name, missing) for missing in sorted(borrowed - late))
    assert offenders == [], "borrowed patchable names not resolved late: %s" % offenders


def test_the_known_tool_cache_has_one_owner():
    """The lazy tool-name cache must not be duplicated across satellites."""
    owners = []
    for name in SATELLITES:
        for node in _parse(_satellite_path(name)).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "_KNOWN_TOOL_NAMES":
                    owners.append(name)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "_KNOWN_TOOL_NAMES":
                        owners.append(name)
    assert owners == ["orchestrator_defaults"], owners


def test_no_satellite_imports_the_facade_at_module_level():
    """An implementation -> facade import at import time closes a cycle."""
    offenders = []
    for name in SATELLITES:
        for node in _parse(_satellite_path(name)).body:
            if isinstance(node, ast.ImportFrom) and node.module == "core.orchestrators":
                offenders.append(name)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "core.orchestrators":
                        offenders.append(name)
    assert offenders == [], "module-level imports of the facade: %s" % offenders


def test_every_satellite_exists_and_imports():
    """A satellite that vanished or fails to import breaks the API."""
    missing = [name for name in SATELLITES if not os.path.isfile(_satellite_path(name))]
    assert missing == [], "satellite modules missing: %s" % missing
    for name in SATELLITES:
        module = _satellite(name)
        assert module.__name__ == "core." + name


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
