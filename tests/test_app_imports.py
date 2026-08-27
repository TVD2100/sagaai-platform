"""
test_app_imports.py - verify that all key project packages
import cleanly under various sys.path conditions.

These tests catch ModuleNotFoundError regressions like the one where
"streamlit run app.py" could not find 'core' because the project root
was missing from sys.path.
"""
import os
import sys
import re

# Path to the project root (parent of tests/)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_all_core_packages_importable():
    """
    Ensure every critical package can be imported without errors.
    This is a quick smoke screen against accidental breakage of __init__.py
    or missing dependencies.
    """
    # Ensure project root is in sys.path
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)

    import core
    import core.paths
    import core.config
    import core.crypto
    import core.env_loader
    import core.files
    import core.fs
    import core.i18n
    import core.render
    import core.services
    import core.skills
    import core.threads
    import core.bootstrap
    import core.api_layer

    import ui
    import ui.app
    import ui.components
    import ui.pages

    import storage
    import storage.models
    import storage.db
    import storage.repository

    import dev_agent
    import dev_agent.config
    import dev_agent.backup_manager
    import dev_agent.safe_writer
    import dev_agent.tool_executor
    import dev_agent.workspace_tools
    import dev_agent.agent_loop
    import dev_agent.universal_agent


def test_no_st_get_option_with_two_args():
    """
    Scan all .py files in the project (excluding the test file itself and
    standard exclusions) for calls to st.get_option() with more than one
    argument. In modern Streamlit versions get_option() accepts only a single
    positional argument - the option name. Default values must be handled
    separately (e.g. `st.get_option('opt') or 'default'`).
    """
    this_file = os.path.relpath(__file__, PROJECT_ROOT)
    pattern = re.compile(r"st\.get_option\s*\(\s*['\"][\w.]+['\"]\s*,")

    errors = []

    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in ('.git', '__pycache__', '.venv', 'venv', '.pytest_cache')]

        for fn in filenames:
            if not fn.endswith('.py'):
                continue
            if fn.startswith('._'):
                # AppleDouble sidecar files (OneDrive/Mac) are binary and
                # must never be read as UTF-8 source.
                continue
            filepath = os.path.join(dirpath, fn)
            relpath = os.path.relpath(filepath, PROJECT_ROOT)

            # Skip this test file - its comments contain the pattern itself
            if relpath == this_file:
                continue

            with open(filepath, encoding='utf-8') as f:
                for lineno, line in enumerate(f, 1):
                    if pattern.search(line):
                        errors.append(f"{relpath}:{lineno}: {line.rstrip()}")

    assert not errors, (
        "Found calls to st.get_option() with >=2 arguments. "
        "Replace with e.g. `st.get_option('name') or 'default'`.\n" +
        "\n".join(errors)
    )
