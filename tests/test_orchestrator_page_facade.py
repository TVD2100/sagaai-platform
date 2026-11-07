# -*- coding: utf-8 -*-
"""tests/test_orchestrator_page_facade.py - the facade contract of the page.

``ui.pages.orchestrator`` was split into a facade and ``orchestrator_*``
modules by topic. The split is only safe while four properties hold, and each
one can break silently:

1. every name the page exposed is still reachable at the OLD path - the chat
   feed, the settings tabs and the tests import from there;
2. the facade keeps the bindings and holds no logic of its own, so a page
   change cannot quietly turn it back into a monolith;
3. a replacement installed on the facade reaches the implementation - tests
   patch ``orch_mod.t``, ``orch_mod._do_step`` and swap ``orch_mod.st`` for a
   double, and a helper bound at import time would ignore all three;
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

PAGES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "pages")

#: Modules the page was split into. ``orchestrator.py`` itself is the facade.
SATELLITES = (
    "orchestrator_shared",
    "orchestrator_state",
    "orchestrator_folder",
    "orchestrator_loop",
    "orchestrator_feed",
    "orchestrator_chat",
    "orchestrator_chat_input",
    "orchestrator_settings_models",
    "orchestrator_settings_orch",
    "orchestrator_settings_delegation",
)

#: Functions the facade is allowed to define itself: the two page entry
#: points. Everything else must be a one-line wrapper over ``<name>_impl``.
ENTRY_POINTS = ("page_orchestrator", "page_devagent")


def _parse(path):
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _satellite_path(name):
    return os.path.join(PAGES_DIR, name + ".py")


def _page():
    """Return the page module the satellites currently resolve through."""
    return importlib.import_module("ui.pages.orchestrator")


def _shared():
    """Return the late-binding layer of the current import tree."""
    return importlib.import_module("ui.pages.orchestrator_shared")


def _satellite(name):
    """Return one satellite of the current import tree."""
    return importlib.import_module("ui.pages." + name)


def _facade_functions(page):
    return [node for node in _parse(page.__file__).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


@pytest.fixture(autouse=True)
def _consistent_import_tree(isolated_app_modules):
    """Start every test from one self-consistent copy of the package.

    The page resolves patchable names through the facade object bound to its
    own module tree. A file that reloaded the package earlier leaves a second
    live copy in the process, and a test that mixes objects from both trees
    would check a facade nobody renders.
    """
    yield


# ─── the public path survives ────────────────────────────────────────────────


def test_every_declared_name_is_reachable():
    """``__all__`` is the executable copy of the page contract."""
    page = _page()
    declared = list(getattr(page, "__all__", []) or [])
    assert declared, "the facade declares no __all__"
    missing = [name for name in declared if not hasattr(page, name)]
    assert missing == [], "names lost in the split: %s" % missing


def test_the_page_entry_points_are_callable():
    """The app dispatches through these names."""
    page = _page()
    for name in ENTRY_POINTS:
        assert callable(getattr(page, name, None)), "%s is not callable" % name


# ─── the facade stays a facade ───────────────────────────────────────────────


def test_the_facade_holds_only_wrappers_and_the_entry_points():
    """Logic belongs to a satellite; the facade only binds and delegates."""
    offenders = []
    for node in _facade_functions(_page()):
        if node.name in ENTRY_POINTS:
            continue
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
    for node in _facade_functions(_page()):
        if node.name in ENTRY_POINTS:
            continue
        returns = [stmt for stmt in ast.walk(node) if isinstance(stmt, ast.Return)]
        called = [stmt.value.func.id for stmt in returns
                  if isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Name)]
        if called != [node.name + "_impl"]:
            wrong.append((node.name, called))
    assert wrong == [], "wrappers that do not delegate to <name>_impl: %s" % wrong


def test_the_facade_stays_small():
    """A facade that grows back into a monolith defeats the split."""
    with open(_page().__file__, encoding="utf-8") as handle:
        assert len(handle.read().splitlines()) < 700


# ─── a patch on the facade reaches the implementation ────────────────────────


def test_a_replaced_translation_reaches_the_satellites():
    """``orch_mod.t = ...`` must be the function the satellites call."""
    page = _page()
    feed = _satellite("orchestrator_feed")

    original = page.t
    page.t = lambda key, **kwargs: "patched:" + str(key)
    try:
        assert feed.t("some_key") == "patched:some_key"
    finally:
        page.t = original


def test_a_replaced_page_helper_reaches_the_satellites():
    """``patch.object(orch_mod, '_do_step', ...)`` must steer the chat tab."""
    page = _page()
    chat = _satellite("orchestrator_chat")

    original = page._do_step
    page._do_step = lambda *args, **kwargs: "replaced"
    try:
        assert chat._do_step("slug") == "replaced"
    finally:
        page._do_step = original


def test_the_streamlit_proxy_reads_the_facade():
    """Swapping ``orch_mod.st`` for a double must reach every satellite."""
    page = _page()
    chat_input = _satellite("orchestrator_chat_input")

    class Double:
        session_state = {"probe": 1}

    original = page.st
    page.st = Double()
    try:
        assert chat_input.st.session_state is Double.session_state
    finally:
        page.st = original


def test_the_shared_layer_resolves_the_current_page():
    """The layer must hand the satellites the page of their own tree."""
    page = _page()
    shared = _shared()

    assert shared.facade() is page
    assert getattr(shared.facade(), "__name__", "") == "ui.pages.orchestrator"


# ─── satellites declare their patchable names late ───────────────────────────


def test_every_satellite_resolves_streamlit_through_the_proxy():
    """A module-level ``import streamlit as st`` freezes the double away."""
    offenders = []
    for name in SATELLITES:
        for node in _parse(_satellite_path(name)).body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "streamlit":
                        offenders.append(name)
            if isinstance(node, ast.ImportFrom) and node.module == "streamlit":
                offenders.append(name)
    assert offenders == [], "satellites importing streamlit directly: %s" % offenders


def test_no_satellite_imports_the_facade_at_module_level():
    """An implementation -> facade import at import time closes a cycle."""
    offenders = []
    for name in SATELLITES:
        for node in _parse(_satellite_path(name)).body:  # module level only: a
            # lazy import inside a function is how the shared layer resolves.
            if isinstance(node, ast.ImportFrom) and node.module == "ui.pages.orchestrator":
                offenders.append(name)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "ui.pages.orchestrator":
                        offenders.append(name)
    assert offenders == [], "module-level imports of the facade: %s" % offenders


def test_every_satellite_exists_and_imports():
    """A satellite that vanished or fails to import breaks the page."""
    missing = [name for name in SATELLITES if not os.path.isfile(_satellite_path(name))]
    assert missing == [], "satellite modules missing: %s" % missing
    for name in SATELLITES:
        module = _satellite(name)
        assert module.__name__ == "ui.pages." + name


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
