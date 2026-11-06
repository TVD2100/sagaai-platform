# -*- coding: utf-8 -*-
"""
dev_agent.loop.steps -- one iteration of the agent loop, phase by phase.

The loop used to be a single 963-line function whose nested closures held the
whole flow: prepare, call the model, parse the answer, execute the calls,
decide how to finish. Each phase is a module here and each function receives
its state and a :class:`LoopRuntime` explicitly, so a phase can be read,
tested and changed on its own.

``runtime`` holds the two dependencies that the facade owns and that a test
may replace THERE: ``send_request`` (patched by most loop tests) and
``drain_notices``. The facade rebuilds the runtime from its own module-level
attributes on every step, so a replacement made through ``agent_loop`` stays
effective without the steps importing the facade.

The remaining helpers (signals, parsing, the economy window, delegation) are
imported directly from their modules: they are patched through their own
module, not through the facade.
"""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
