# -*- coding: utf-8 -*-
"""
The explicit dependencies of one loop step.

A step needs a few things the facade owns: the tool dispatcher, the language,
the event sink, and the two callables a test may replace on the facade module
(``send_request`` and the subagent-notice drain). Passing them as one object
keeps the signatures of the phase functions short and makes the dependency
visible at the call site instead of hiding it in a closure.

:func:`build_runtime` is the only place that reads those callables, and the
facade calls it on every step - so a replacement made through the facade
module is picked up by the next step.
"""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class LoopRuntime:
    """Everything a phase of one step needs besides the state itself.

    :param dispatcher: the tool dispatcher of the run.
    :param lang: the answer language, or ``None`` for the default.
    :param on_event: the raw event sink of the run (see :meth:`emit`).
    :param send_request: the model call; replaced by tests on the facade.
    :param drain_notices: moves finished-child notices into the next call.
    """

    dispatcher: Any
    lang: Optional[str] = None
    on_event: Optional[Callable[[dict], None]] = None
    send_request: Optional[Callable[..., str]] = None
    drain_notices: Optional[Callable[[Any], List[str]]] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def emit(self, state: Any, event: dict) -> None:
        """Stamp the delegation identity onto *event* and pass it to the sink.

        The UI groups the feed by run and draws the parent-child tree from
        these fields, and a subscriber must be able to tell a step of its own
        run from a forwarded event of a descendant. Plain dialogs carry empty
        values, so the stamping is a no-op for them.
        """
        if state.run_id and "run_id" not in event:
            event["run_id"] = state.run_id
        if state.parent_run_id and "parent_run_id" not in event:
            event["parent_run_id"] = state.parent_run_id
        if state.depth and "depth" not in event:
            event["depth"] = state.depth
        if self.on_event:
            self.on_event(event)

    def require_send_request(self) -> Callable[..., str]:
        """Return the model call, or raise when the runtime has none."""
        if self.send_request is None:
            raise RuntimeError("the loop runtime has no send_request callable")
        return self.send_request

    def drain(self, state: Any) -> List[str]:
        """Drain the finished-child notices of *state* (empty when unbound)."""
        if self.drain_notices is None:
            return []
        return list(self.drain_notices(state) or [])


def build_runtime(
    *,
    dispatcher: Any,
    lang: Optional[str],
    on_event: Optional[Callable[[dict], None]],
    send_request: Optional[Callable[..., str]],
    drain_notices: Optional[Callable[[Any], List[str]]],
) -> LoopRuntime:
    """Assemble the runtime of one step from the facade's current attributes."""
    return LoopRuntime(
        dispatcher=dispatcher,
        lang=lang,
        on_event=on_event,
        send_request=send_request,
        drain_notices=drain_notices,
    )
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
