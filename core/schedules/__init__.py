# -*- coding: utf-8 -*-
"""
core.schedules - scheduled agent runs (the "scheduled runs" feature).

Pure business logic except the daemon and the runner, which start threads:

- ``model`` - the Schedule dataclass, its validation and DB mapping;
- ``calc`` - the pure ``compute_next_run`` grid calculation (no clock, no DB);
- ``store`` - SQLite persistence, the atomic due-point claim and run history;
- ``runner`` - headless execution of one scheduled run as a REAL dialog;
- ``daemon`` - the in-process tick loop that fires due schedules.

A scheduled run creates an ordinary dialog thread (kind=dialog, no parent),
so it appears in the History page like any other dialog and can be reopened
and read there.
"""
from __future__ import annotations

__all__ = ["model", "calc", "store", "runner", "daemon"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
