# -*- coding: utf-8 -*-
"""
core.orchestrator_steps_migration - upgrade of a retired default step limit.

The platform ships a default autonomous-loop budget, and that number has been
raised over time. A record that still carries a number which once WAS the
default never chose it, so the bootstrap raises such a record to the current
default exactly once. Anything else - a limit the owner typed - is a decision
and is never overwritten.

A config marker makes the upgrade one-shot: an owner who deliberately sets
the limit back to a retired number keeps it across later boots.
"""
from typing import Any, Dict

#: Step limits that shipped as the platform default and were later raised.
RETIRED_MAX_STEPS = (100, 500)

#: Config marker that records the migration has run for this record.
MAX_STEPS_MIGRATED_KEY = "max_steps_default_migrated"


def migrate_max_steps(config: Dict[str, Any], existing: Dict[str, Any]) -> bool:
    """Mark *config* when *existing* still carries a retired step limit.

    The caller writes the config back with the current default; this helper
    only decides whether that must happen and records the decision.

    Args:
        config: the config mapping that is about to be saved (mutated).
        existing: the stored orchestrator record.

    Returns:
        True when the limit has to be raised to the current default.
    """
    if config.get(MAX_STEPS_MIGRATED_KEY):
        return False
    try:
        stored = int(existing.get("max_steps"))
    except (TypeError, ValueError):
        return False
    if stored not in RETIRED_MAX_STEPS:
        return False
    config[MAX_STEPS_MIGRATED_KEY] = True
    return True


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
