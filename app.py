"""
app.py — thin entry point for the SagaAI Streamlit application.
Sets page config then delegates to ui.app.main().
"""
import os
import sys

# Ensure the project root is on sys.path so that 'core' and 'ui' can be imported
# when running via `streamlit run app.py` (which does not add cwd automatically).
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.updater import apply_updates, write_running_marker  # noqa: E402

# Cold-start update hook: apply staged downloads while the app is not running
# yet, then mark this process as live so the CLI refuses in-place apply.
apply_updates(_project_root)
write_running_marker(_project_root)

import streamlit as st

st.set_page_config(
    page_title="SagaAI Assistant",
    page_icon=os.path.join(_project_root, "assets", "favicon.svg"),
    layout="wide",
    initial_sidebar_state="expanded",
)

from core.paths import ensure_data_dirs  # noqa: E402 — after st.set_page_config
from ui.app import main                  # noqa: E402

ensure_data_dirs()

if __name__ == "__main__":
    main()
else:
    main()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
