"""The runtime dependencies a tool method needs, resolved at call time.

A tool body never reads a dependency from its own module globals: tests
replace those names on the facade module (``patch('dev_agent.tool_executor
.send_request')``), and a module-level import here would hide the patch. The
bundle is rebuilt on every access on purpose - a handler can be invoked
directly, bypassing ``dispatch``, so there is nothing to prime it from.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Names read from the facade module on every tool call. Each one is a patch
#: point in the test suite, so the lookup must stay dynamic.
DEP_NAMES = (
    'config',
    'send_request',
    'load_config',
    'load_devagent_config',
    'get_services',
    'call_llm_with_system',
    'get_instruction_prompt',
    'classify_assistant_requirements',
    'resolve_service_model_for_assistant',
    'create_assistant',
    'get_assistant_by_id',
    'detect_and_select_assistant',
    'list_all_assistants_for_detection',
    'datetime',
    'subprocess',
)


@dataclass(frozen=True)
class ToolDeps:
    """Snapshot of the facade module attributes one tool call depends on."""

    config: Any = None
    send_request: Any = None
    load_config: Any = None
    load_devagent_config: Any = None
    get_services: Any = None
    call_llm_with_system: Any = None
    get_instruction_prompt: Any = None
    classify_assistant_requirements: Any = None
    resolve_service_model_for_assistant: Any = None
    create_assistant: Any = None
    get_assistant_by_id: Any = None
    detect_and_select_assistant: Any = None
    list_all_assistants_for_detection: Any = None
    datetime: Any = None
    subprocess: Any = None


def facade():
    """Return the executor facade module.

    Imported lazily: the facade imports every module of this package, so a
    module-level import here would close the cycle.
    """
    from .. import tool_executor
    return tool_executor


def build_deps() -> ToolDeps:
    """Read the current value of every patched name from the facade module."""
    module = facade()
    values = {name: getattr(module, name, None) for name in DEP_NAMES}
    if values.get('datetime') is None:
        # The facade has no module-level ``datetime`` binding; the clock tool
        # falls back to the real class so it always has a working clock.
        from datetime import datetime as _datetime
        values['datetime'] = _datetime
    return ToolDeps(**values)


class ToolMixin:
    """Base of every tool mixin: gives handlers access to the dependencies."""

    def deps(self) -> ToolDeps:
        """Return the dependency bundle for this call (never cached)."""
        return build_deps()


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
