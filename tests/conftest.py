"""
conftest.py - shared test fixtures and hooks.
"""
import pytest


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
