"""
core.gateway - the single entry point into the core for non-UI transports.

The streamlit page, the schedule daemon and the HTTP server all need the
same three things from the platform: start a dialog, read what happened,
and look up the entities they may address. Until now each of them reached
into ``core`` and ``dev_agent`` directly, which is how the root dialog run
ended up implemented twice and why a third transport had nowhere to go.

Layering rule, and it is checked by a test:

    server/        ->  core.gateway  ->  core.orchestration  ->  dev_agent
    ui/            ->  core.orchestration (v1: unchanged)
    core/schedules ->  dev_agent       (v1: unchanged)

This package imports NEITHER streamlit, NOR fastapi, NOR pydantic. It is
plain Python with dataclasses, so the same code serves a CLI, a test and
the HTTP layer.

What lives where:

- ``dto``       - every transport type, and the only home of one;
- ``dialog``    - create, start, advance and continue a root dialog;
- ``runs``      - status, waiting, cancellation and answers;
- ``threads``   - reading dialogs and their messages;
- ``entities``  - employees and assistants (read-only in v1);
- ``health``    - liveness, readiness and the reported version;
- ``errors``    - the one error type and its HTTP translation.
"""

from __future__ import annotations

from core.gateway.dto import (  # noqa: F401 - public surface
    AssistantCard,
    AssistantDetail,
    DialogHandle,
    DialogOutcome,
    DialogRequest,
    DialogSnapshot,
    DialogStepResult,
    EconomyOptions,
    HealthReport,
    MessageView,
    OrchestratorCard,
    OrchestratorDetail,
    PendingInput,
    RunSnapshot,
    SearchPage,
    ThreadPage,
    ThreadSummary,
    ThreadWindow,
)
from core.gateway.errors import GatewayError  # noqa: F401 - public surface

__all__ = [
    "AssistantCard",
    "AssistantDetail",
    "DialogHandle",
    "DialogOutcome",
    "DialogRequest",
    "DialogSnapshot",
    "DialogStepResult",
    "EconomyOptions",
    "GatewayError",
    "HealthReport",
    "MessageView",
    "OrchestratorCard",
    "OrchestratorDetail",
    "PendingInput",
    "RunSnapshot",
    "SearchPage",
    "ThreadPage",
    "ThreadSummary",
    "ThreadWindow",
    "backup",
    "bootstrap",
    "dialog",
    "diagnostics",
    "dto",
    "entities",
    "errors",
    "health",
    "runs",
    "threads",
]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
