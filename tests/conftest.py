"""
conftest.py - shared test fixtures and hooks.
"""
import pytest


@pytest.fixture(autouse=True)
def _restore_dev_agent_state():
    """Restore the process-global DevAgent workspace state after EVERY test.

    Rendering orchestrator pages, resetting dialogs and switching workspaces
    mutate the module-level state of ``dev_agent.config`` (PROJECT_ROOT,
    WORKSPACE_SELECTED, runtime dirs, ...) and the workspace-binding registry.
    A leaked ``WORKSPACE_SELECTED=False`` used to block file tools (the
    empty-state guard) in unrelated files later in the same pytest run, so
    the state is snapshotted before each test and restored afterwards.
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
