# -*- coding: utf-8 -*-
"""
UI smoke tests for the employee management pages.

Verifies that the employee management page (orchestrators) and the per-employee
settings page render without errors and that the export/import employee UI has
been removed from those pages.
"""
import os
import sys
import importlib
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)              # .../sagaai
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture()
def isolated_data(monkeypatch):
    """Point the app at a throwaway data dir."""
    d = tempfile.mkdtemp()
    monkeypatch.setenv("SAGAAI_DATA_DIR", d)

    original_modules = {
        name: mod
        for name, mod in list(sys.modules.items())
        if name.startswith(("core", "storage", "ui"))
    }
    for m in list(sys.modules):
        if m.startswith(("core", "storage", "ui")):
            sys.modules.pop(m, None)

    try:
        yield d
    finally:
        for name, mod in original_modules.items():
            sys.modules[name] = mod


def _fresh_ui():
    """Reimport ui modules fresh under the active streamlit mock."""
    for m in list(sys.modules):
        if m.startswith(("core", "storage", "ui")):
            sys.modules.pop(m, None)
    return importlib.import_module("ui.app")


def test_orchestrators_management_page_renders(isolated_data):
    """Employee management page must render without errors and without
    export/import widgets."""
    with install_streamlit_mock() as st:
        ui_app = _fresh_ui()
        from ui.pages.orchestrators import page_orchestrators

        st.session_state.update(dict(ui_lang="English", current_page="orchestrators"))
        try:
            page_orchestrators()
        except StopRerun:
            pass
        assert st.errors == [], f"orchestrators page emitted errors: {st.errors}"

        for call in st.calls:
            k = call[2].get("key", "") if len(call) > 2 else ""
            assert "export" not in str(k).lower(), f"export widget found: {k}"
            assert "import" not in str(k).lower(), f"import widget found: {k}"


def test_orchestrator_settings_page_has_no_export_import_tab(isolated_data):
    """Per-employee settings page must render without errors and without the
    Export/Import tab."""
    with install_streamlit_mock() as st:
        ui_app = _fresh_ui()

        # Seed the built-in DevAgent orchestrator so get_orchestrator()
        # resolves for the settings page.
        from core.bootstrap import ensure_devagent_settings
        ensure_devagent_settings()

        from ui.pages.orchestrator_settings import page_orchestrator_settings

        st.session_state.update(
            dict(ui_lang="English", current_page="orchestrator_settings:dev_agent")
        )
        try:
            page_orchestrator_settings("dev_agent")
        except StopRerun:
            pass
        assert st.errors == [], f"settings page emitted errors: {st.errors}"

        button_labels = []
        for call in st.calls:
            if call[0] == "button" and call[1]:
                button_labels.append(str(call[1][0]))
        exported = [lbl for lbl in button_labels if "export" in lbl.lower()]
        imported = [lbl for lbl in button_labels if "import" in lbl.lower()]
        assert not exported, f"export buttons still present: {exported}"
        assert not imported, f"import buttons still present: {imported}"


def test_no_export_import_employee_ui_in_code():
    """No UI module may reference employee export/import machinery anymore."""
    ui_dir = os.path.join(PKG_ROOT, "ui")
    for root, _dirs, files in os.walk(ui_dir):
        for f in files:
            if not f.endswith(".py"):
                continue
            if f.startswith("._"):
                # AppleDouble sidecar files (OneDrive/Mac) are binary and
                # must never be read as UTF-8 source.
                continue
            p = os.path.join(root, f)
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
            for needle in (
                "_render_export_import",
                "orch_tab_export_import",
                "export_orchestrator",
                "import_orchestrator",
                "orch_export_btn",
                "orch_import_btn",
                "orch_export_import_section",
                "orch_mgmt_import_upload",
            ):
                assert needle not in text, f"{needle!r} still referenced in {p}"
