"""
app.py - thin entry point for the SagaAI Streamlit application.
Sets page config then delegates to ui.app.main().
"""
import os
import sys

# Ensure the project root is on sys.path so that 'core' and 'ui' can be imported
# when running via `streamlit run app.py` (which does not add cwd automatically).
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.updater import apply_updates, same_process_marker, write_running_marker  # noqa: E402

# Cold-start update hook: apply staged downloads while the app is not running
# yet, then mark this process as live so the CLI refuses in-place apply.
# Streamlit re-executes app.py on every rerun inside the same process, so the
# marker identifies reruns and the hook runs only on the real cold start.
# The same flag gates the database-format check below: both must run once per
# process, not once per rerun.
_cold_start = not same_process_marker(_project_root)
if _cold_start:
    _apply_report = apply_updates(_project_root)
    print(
        "[cold-start apply] ok=%s applied=%r failed=%r error=%r"
        % (
            _apply_report.get("ok"),
            _apply_report.get("applied") or [],
            _apply_report.get("failed"),
            _apply_report.get("error"),
        ),
        file=sys.stderr,
        flush=True,
    )
    write_running_marker(_project_root)

# A marketplace run interrupted by a crash is undone before the UI opens.
# The recovery lives in its own ``try`` because an unhandled failure here
# would kill the start of the whole application, while the run it restores
# only affects one installed entity.
try:
    from core.marketplace import bootstrap as marketplace_bootstrap

    _marketplace_recovery = marketplace_bootstrap.recover()
    if _marketplace_recovery:
        print(
            "[marketplace recovery] recovered=%r" % (_marketplace_recovery,),
            file=sys.stderr,
            flush=True,
        )
except Exception as _marketplace_error:  # pragma: no cover - start-up guard
    print(
        "[marketplace recovery] error=%r" % (_marketplace_error,),
        file=sys.stderr,
        flush=True,
    )

# Both databases are brought to the current schema revision before anything
# reads them. The driver runs the message-layout archivers first and then the
# revision chain, so the order the platform depends on lives in one place
# instead of being repeated by every transport.
if _cold_start:
    try:
        from storage import migrations as _migrations

        _migration_report = _migrations.upgrade_installation()
        if not _migration_report.get("ok"):
            print(
                "[schema] error=%r" % (_migration_report,),
                file=sys.stderr,
                flush=True,
            )
        else:
            for _item in _migration_report.get("databases") or []:
                if _item.get("action") not in ("none", "unchanged"):
                    print(
                        "[schema] %s action=%s revision=%s"
                        % (_item.get("kind"), _item.get("action"),
                           _item.get("after")),
                        file=sys.stderr,
                        flush=True,
                    )
    except Exception as _migration_error:  # pragma: no cover - start-up guard
        print(
            "[schema] error=%r" % (_migration_error,),
            file=sys.stderr,
            flush=True,
        )

import streamlit as st

st.set_page_config(
    page_title="SagaAI Platform",
    page_icon=os.path.join(_project_root, "assets", "favicon.svg"),
    layout="wide",
    initial_sidebar_state="expanded",
)

from core.paths import ensure_data_dirs  # noqa: E402 - after st.set_page_config
from ui.app import main  # noqa: E402

ensure_data_dirs()

# The scheduler lives in this process: it keeps firing while the platform is
# open and dies with it, so nothing is left running behind the owner's back.
# Streamlit re-executes this module on every rerun; ``bootstrap`` is idempotent
# and returns the loop that is already running, so calling it unconditionally
# costs one lookup and guarantees the loop comes back even if the very first
# start happened while the database was still being prepared. Both the import
# and the start are guarded: a broken scheduler must never keep the platform
# shut, and the page then reports honestly that nothing watches the clock.
try:
    from core.schedules.daemon import bootstrap as _start_scheduler

    _scheduler = _start_scheduler()
except Exception as _scheduler_error:  # pragma: no cover - start-up guard
    _scheduler = None
    print(
        "[scheduler] error=%r" % (_scheduler_error,),
        file=sys.stderr,
        flush=True,
    )
if _cold_start:
    print(
        "[scheduler] started=%s" % (bool(_scheduler),),
        file=sys.stderr,
        flush=True,
    )

if __name__ == "__main__":
    main()
else:
    main()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
