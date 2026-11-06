# -*- coding: utf-8 -*-
"""
dev_agent.loop -- building blocks of the provider-independent agent loop.

The package splits the working cycle into focused modules:

* ``state``     -- ``AgentLoopState`` (mutable state of one run) and
                   ``AgentResult`` (the value a finished run returns).
* ``notices``   -- delivery of finished-subagent notices into the loop.
* ``results``   -- the size policy applied to tool results.
* ``json_repair`` -- recovery of malformed JSON in a model answer.
* ``signals``    -- reading the answer: loop_status, plan/question detection, step strength.
* ``context``    -- what the loop injects: journal and thread snapshots, economy window.
* ``delegation`` -- the fan-out phase: delegation calls, child identity, parallel batch.
* ``parse``     -- turning a model answer into tool calls (JSON repair, DSML).

The public surface of ``dev_agent.agent_loop`` stays unchanged: the facade
module re-exports every name defined here, so existing imports and the
tests that reach into the loop module keep working.
"""
from __future__ import annotations

__all__ = ["json_repair", "notices", "parse", "results", "state"]

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
