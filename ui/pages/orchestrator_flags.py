# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_flags - The two independent protection switches.

Execution confirmation and content sanitisation are separate layers. A user
who wants fewer confirmation dialogs must not lose prompt-injection
protection as a side effect, so each switch owns its own session slot, its
own widget key and its own syncing into the running loop state.

* ``safety_mode`` - ask the user before a dangerous execution;
* ``injection_protection`` - sanitise external content before it reaches the
  model;
* ``sandbox`` - run generated code isolated from the machine. It is the only
  switch that changes HOW a permitted run executes, so it reads the profile
  stored in the employee settings instead of owning a second definition.
"""

from typing import Optional, Tuple

#: Default of each switch when the session slot was never written.
PROTECTION_DEFAULTS = {
    "safety_mode": True,
    "injection_protection": True,
}

PROTECTION_ORDER = ("safety_mode", "injection_protection")

#: The three states the access badge can show. ``full`` is the grant of the
#: platform environment, ``partial`` is a legacy record whose isolation was
#: cleared before the grant existed, ``isolated`` is everything else.
ACCESS_FULL = "full"
ACCESS_PARTIAL = "partial"
ACCESS_ISOLATED = "isolated"


def _flag_false(value) -> bool:
    """True when *value* is an explicit off, in any stored form."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in ("0", "false", "no", "off")
    return value is False


def access_mode(read_orchestrator, slug: str) -> str:
    """Return the access state of this employee.

    The grant is the only thing that gives the platform environment to a
    child. A record that cleared the old toolbar checkbox before the grant
    existed keeps its isolation off but its environment filtered: reporting
    that as full access would hand out rights the owner never gave, and
    reporting it as isolated would hide the fact that the machine is open.
    """
    if env_isolation(read_orchestrator, slug) is False:
        return ACCESS_FULL
    orch = read_orchestrator(slug) or {}
    if _flag_false((orch.get("config") or {}).get("chat_sandbox")):
        return ACCESS_PARTIAL
    return ACCESS_ISOLATED


def toolbar_specs(t, lang: str) -> tuple:
    """Return ``(slot, label, help)`` for each toolbar checkbox.

    The specs live here, next to the defaults they describe, so the toolbar
    itself stays a rendering function and each preference keeps one owner.

    The old sandbox checkbox is gone: it and the profile selector were two
    controls for one fact, and neither said what the user actually chooses -
    whether the agent may reach the machine. That is now a single setting
    with a read-only badge in the toolbar (see ``access_mode``).
    """
    return (
        ("web_search", t("orch_web_search_label", lang=lang),
         t("orch_web_search_help", lang=lang)),
        ("safety_mode", t("orch_safety_label", lang=lang),
         t("orch_safety_help", lang=lang)),
        ("injection_protection", t("orch_injection_label", lang=lang),
         t("orch_injection_help", lang=lang)),
    )


def sandbox_profile(read_orchestrator, slug: str) -> str:
    """Return the execution profile stored in the employee config.

    An empty value means the policy default; the name is NOT validated here,
    because an unknown profile must reach the policy and be refused there.
    """
    orch = read_orchestrator(slug) or {}
    return str((orch.get("config") or {}).get("sandbox_profile") or "")


def env_isolation(read_orchestrator, slug: str) -> bool:
    """Return whether the child of this employee gets the filtered environment.

    An empty or unreadable config means the isolated form: the grant must be
    an explicit choice, never the result of a missing value.
    """
    orch = read_orchestrator(slug) or {}
    value = (orch.get("config") or {}).get("env_isolation")
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def effective_sandbox_profile(read_orchestrator, slug: str,
                              enabled: Optional[bool] = None) -> str:
    """Return the profile a run of this employee executes under.

    The toolbar checkbox is the switch the user owns; ``None`` means the slot
    has not been written yet and the switch defaults to on. While it is on,
    the profile comes from the employee settings, so the checkbox never
    overrides a deliberate choice of profile. While it is off, the run uses
    the ``off`` profile: no OS isolation, but the same resource limits and
    the same built environment, because a runaway child must not be able to
    take the application down.

    The full-access grant also selects the ``off`` profile: a run that
    receives the platform environment must not be confined by a boundary
    whose policy would refuse the very writes such a task needs.

    ``enabled=None`` means the session slot was never written - the checkbox
    no longer exists, so the stored ``chat_sandbox`` of the config is the
    remaining record of that choice and is honoured until the user picks a
    mode explicitly.
    """
    from core.sandbox.profiles import OFF

    if env_isolation(read_orchestrator, slug) is False:
        return OFF
    if enabled is None:
        orch = read_orchestrator(slug) or {}
        enabled = (orch.get("config") or {}).get("chat_sandbox")
    if _flag_false(enabled):
        return OFF
    return sandbox_profile(read_orchestrator, slug)


def read_protection_flags(read) -> Tuple[bool, bool]:
    """Return ``(safety_mode, injection_protection)`` from a slot reader.

    *read* is a callable that returns the value of one session slot by name;
    the caller owns the storage, this module owns the defaults and the rule
    that the two switches never share a value.
    """
    values = []
    for name in PROTECTION_ORDER:
        raw = read(name)
        values.append(PROTECTION_DEFAULTS[name] if raw is None else bool(raw))
    return values[0], values[1]

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
