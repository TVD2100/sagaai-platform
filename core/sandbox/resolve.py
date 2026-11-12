# -*- coding: utf-8 -*-
"""The execution profile and the safety switch of one employee.

Every place that creates a dispatcher asks this module instead of reading
``orchestrator.config`` itself: the child runner, the dialog runner, the
schedule runner and the chat page must all reach the same answer, otherwise
the user's choice reaches one of them and stops at the others. That was the
defect this module exists to close: the toolbar checkbox was applied to the
dialog dispatcher only, so a delegated child kept running under the default
profile with the dangerous-code gate on, whatever the user had chosen.

Two independent preferences live in the employee config:

* ``chat_sandbox`` - the toolbar switch. ``False`` means the ``off`` profile:
  no operating-system isolation, while the resource limits and the built
  environment still apply.
* ``chat_safety_mode`` - the dangerous-code gate. ``False`` means an execution
  tool runs the analysed text without asking for a confirmation.

They are independent on purpose: turning isolation off must not silently
drop the confirmation, and turning confirmations off must not silently drop
isolation. The two are read by separate functions so neither can borrow the
other's value by accident.
"""
from __future__ import annotations

from typing import Any, Dict

from core.sandbox.profiles import OFF

#: Strings a stored boolean may arrive as (a hand-edited config, an older
#: record written by a form). Anything else is read through ``bool``.
_FALSE_WORDS = ("0", "false", "no", "off", "")


def _as_bool(value: Any, default: bool) -> bool:
    """Return *value* as a boolean, honouring the string forms of "false"."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    return bool(value)


def employee_config(slug: str) -> Dict[str, Any]:
    """Return the config mapping of *slug*, or an empty one.

    A missing employee and an unreadable store both yield an empty mapping,
    so the caller falls back to the documented defaults instead of failing a
    run because a settings lookup went wrong.
    """
    if not slug:
        return {}
    try:
        from core.orchestrators import get_orchestrator

        orch = get_orchestrator(slug) or {}
    except Exception:  # pragma: no cover - a broken store is not a run error
        return {}
    config = orch.get("config") if isinstance(orch, dict) else None
    return dict(config) if isinstance(config, dict) else {}


def sandbox_profile(slug: str) -> str:
    """Return the profile a run of *slug* executes under.

    An empty string means "the policy default". It is returned as is rather
    than resolved here: an unknown name stored in the settings must reach the
    policy and be refused there, not be replaced by a permissive default.
    """
    config = employee_config(slug)
    if _as_bool(config.get("chat_sandbox"), True) is False:
        return OFF
    return str(config.get("sandbox_profile") or "")


def safety_enabled(slug: str) -> bool:
    """Return whether the dangerous-code gate is on for *slug*.

    The default is on: an install that never wrote the key keeps asking for
    a confirmation before an analysed snippet runs.
    """
    return _as_bool(employee_config(slug).get("chat_safety_mode"), True)


def apply_to(dispatcher: Any, slug: str) -> None:
    """Stamp the resolved preferences onto a freshly built dispatcher.

    Both attributes are read by the execution tools at call time, so setting
    them once here covers every ``run_code`` and ``run_test`` of the run.
    """
    core = getattr(dispatcher, "core", None)
    if core is None:  # pragma: no cover - every dispatcher has a core
        return
    core._sandbox_profile = sandbox_profile(slug)
    core._safety_enabled = safety_enabled(slug)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
