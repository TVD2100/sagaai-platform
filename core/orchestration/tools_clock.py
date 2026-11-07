# -*- coding: utf-8 -*-
"""core.orchestration.tools_clock - the clock the waiting tools read.

The wait tools must use the SAME ``time`` binding the rest of the facade
exposes: a test replaces ``tools.time`` with a controllable clock, and a
module that imported ``time`` for itself would keep sleeping in real time.
The attribute is therefore read through the facade at CALL time, not bound
at import time.
"""

from __future__ import annotations


def _time():
    """Return the ``time`` binding of the facade module."""
    from core.orchestration import tools as facade
    return facade.time


def monotonic() -> float:
    """Read the monotonic clock of the facade."""
    return _time().monotonic()


def wall_time() -> float:
    """Read the wall clock of the facade."""
    return _time().time()


def sleep(seconds: float) -> None:
    """Sleep on the clock of the facade."""
    _time().sleep(seconds)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
