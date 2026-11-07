# -*- coding: utf-8 -*-
"""
core.orchestrator_shared - late-binding layer of the orchestrator API.

The API is split by theme across the ``core.orchestrator_*`` modules. The tests
address it through the facade module: ``monkeypatch.setattr(orch_mod,
"get_orchestrator", ...)``, ``patch.object(orch_mod, "_sync_orchestrator_folder",
...)``, ``patch("core.orchestrators.get_orchestrator")``. A helper moved into its
own module would read its OWN globals and stop seeing a patch installed on the
facade, so every name the tests may replace is resolved on the facade AT CALL
TIME instead of being bound at import time.

``late(name)`` returns a callable that looks the attribute up on the facade
module each time it is called. A module declares the name once at its top
(``get_orchestrator = late("get_orchestrator")``) and its body does not change.

The lookup goes through ``sys.modules`` rather than an import of the facade:
the facade imports these modules, so a direct import here would close an import
cycle. The facade binds itself with ``bind_facade`` after it has defined every
wrapper, so a satellite imported while the facade is still loading resolves
through the object its own tree was loaded with.
"""
import sys
from typing import Any, Callable

# The facade module path, resolved lazily: at import time this module is loaded
# BY the facade, so the facade object is not ready yet.
FACADE = "core.orchestrators"


# The facade object this module belongs to. Each facade instance binds itself
# here while it loads, so a module tree that was imported once and later
# replaced in ``sys.modules`` keeps resolving through the object its own
# helpers were loaded with. A test that holds an older module object still
# reaches the implementation behind it.
_BOUND: Any = None


def bind_facade(module: Any) -> None:
    """Attach *module* as the facade this shared module resolves against."""
    global _BOUND
    _BOUND = module


def facade() -> Any:
    """Return the facade module object.

    Resolved on every access: a test may replace the whole module in
    ``sys.modules`` (an isolation fixture does), and the helpers below must see
    the same object the test does. A module tree that bound itself at import
    time wins over the registry, so two live copies of the API do not steal
    each other's patches.
    """
    bound = _BOUND
    if bound is not None:
        return bound
    return sys.modules[FACADE]


def late(name: str) -> Callable[..., Any]:
    """Return a callable that resolves ``name`` on the facade when called.

    Args:
        name: attribute of the facade module to call.

    Returns:
        A thin wrapper with the same call signature as the target.
    """
    def _call(*args: Any, **kwargs: Any) -> Any:
        return getattr(facade(), name)(*args, **kwargs)

    _call.__name__ = name
    _call.__qualname__ = name
    # Marks the wrapper so tools that inspect the API can tell a late reference
    # from a real function object.
    _call.__late_name__ = name  # type: ignore[attr-defined]
    return _call


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
