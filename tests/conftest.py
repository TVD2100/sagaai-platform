"""
conftest.py - shared test fixtures and hooks.
"""
import pytest


@pytest.fixture
def _restore_dev_agent_state():
    """Restore the process-global DevAgent workspace state after a test.

    Rendering orchestrator pages, resetting dialogs and switching workspaces
    mutate the module-level state of ``dev_agent.config`` (PROJECT_ROOT,
    WORKSPACE_SELECTED, runtime dirs, ...) and the workspace-binding registry.
    A leaked ``WORKSPACE_SELECTED=False`` blocks file tools (the empty-state
    guard) in unrelated files later in the same pytest run.

    The fixture is requested EXPLICITLY by the tests that mutate that state:
    an autouse hook hid which tests depend on it and made every test in the
    suite pay for a snapshot it never needed. A test that touches the global
    state asks for this fixture by name; a test that does not, does not.
    """
    from dev_agent import config
    from dev_agent import workspace_binding as wb

    state = config.snapshot_state()
    with wb.sync_lock():
        old_registry = dict(wb._REGISTRY)
    try:
        yield
    finally:
        config.restore_state(state)
        with wb.sync_lock():
            wb._REGISTRY.clear()
            wb._REGISTRY.update(old_registry)


@pytest.fixture
def isolated_app_modules():
    """Isolate project modules (core/storage/ui) for the duration of a test.

    Saves the original module objects, removes them from ``sys.modules`` so
    the test re-imports fresh copies (picking up new env vars), and on exit
    restores the originals while dropping any modules that were created
    during the test. This prevents state leakage between test files.

    Usage:
        def test_x(isolated_app_modules):
            # fresh core/storage/ui modules available here
    """
    from tests._test_isolation import isolated_app_modules as _iso
    with _iso():
        yield
