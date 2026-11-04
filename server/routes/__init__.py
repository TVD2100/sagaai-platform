"""
server.routes - one module per resource group.

Each module builds an ``APIRouter`` and does three things only: validate the
path and body, call the gateway, and map the result through a response
model. Business logic stays below the gateway; a route that starts to make
decisions has drifted.

Groups:

- ``health``  - liveness, readiness and version; NO authentication, because a
  monitoring system has no token and needs none of the data;
- ``agents``  - employees and assistants, read-only in v1;
- ``dialogs`` - launch and continue a dialog;
- ``runs``    - status, wait, cancel, answer;
- ``threads`` - read dialogs and their messages.

Every data route depends on ``server.deps.require_caller``. A route without
it would be an unauthenticated hole, so a test walks the route table and
checks the dependency is present on each one.
"""

from __future__ import annotations

__all__ = ["agents", "dialogs", "health", "runs", "threads"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
