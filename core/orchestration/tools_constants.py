# -*- coding: utf-8 -*-
"""core.orchestration.tools_constants - the numeric ceilings of the tools.

Split out of ``core.orchestration.tools``: the public path of every name
below stays the same, the facade module re-exports it.
"""

from __future__ import annotations

from core.orchestration.contracts import (
    MAX_SUBAGENT_TIMEOUT_SECONDS,
)

MAX_STATUS_WAIT_SECONDS = 30.0

MAX_STATE_CHARS = 6000

MAX_SLEEP_SECONDS = float(MAX_SUBAGENT_TIMEOUT_SECONDS)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
