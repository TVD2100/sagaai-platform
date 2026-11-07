# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_shared - late-binding layer of the orchestrator page.

The page is split into modules by responsibility. The tests address the page
through the module object: ``monkeypatch.setattr(orch_mod, "t", ...)``,
``patch.object(orch_mod, "_do_step", ...)``, ``orch_mod.st = mock_env``. A
helper moved into its own module would read its OWN globals and stop seeing a
patch installed on the facade, so every name the tests may replace is resolved
on the facade AT CALL TIME instead of being bound at import time.

Two accessors cover the whole surface:

- ``late(name)`` - returns a callable that looks the attribute up on the
  facade module each time it is called. A module declares the name once at
  its top (``_do_step = late("_do_step")``) and its body does not change.
- ``st_proxy()`` - returns an attribute proxy over the facade's ``st``, so
  ``st.markdown(...)`` reaches the streamlit module (or the test double)
  that the facade holds right now.

Both resolve through ``sys.modules`` rather than an import of the facade: the
facade imports these modules, so a direct import here would close an import
cycle.
"""
import sys
from typing import Any, Callable

from ui.components.flash import flash_error

# The facade module path, resolved lazily: at import time this module is
# loaded BY the facade, so the facade object is not ready yet.
FACADE = "ui.pages.orchestrator"


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
    ``sys.modules`` (the isolation fixture does), and the helpers below must
    see the same object the test does. A module tree that bound itself at
    import time wins over the registry, so two live copies of the page do not
    steal each other's patches.
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
    # Marks the wrapper so ``tools`` that inspect the page can tell a late
    # reference from a real function object.
    _call.__late_name__ = name  # type: ignore[attr-defined]
    return _call


class _StProxyType:
    """Attribute proxy that reads the facade's ``st`` on every access.

    Reading ``st.session_state`` therefore returns the object the facade
    holds at that moment, which is what lets a test swap the whole streamlit
    module for a double without touching the page modules.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(facade().st, name)

    def __repr__(self) -> str:
        return f"<st proxy -> {getattr(facade().st, '__name__', 'st')}>"


_SHARED_ST = _StProxyType()


def st_proxy() -> _StProxyType:
    """Return the shared streamlit proxy.

    One instance for the whole package: the proxy is stateless and creating a
    new object per module would only add allocation on the render path.
    """
    return _SHARED_ST


# Settings sections in tab order. A save handler pins the section it saved so
# the settings page reopens there after its rerun.
SETTINGS_SECTIONS = (
    "models", "prompt", "economy", "functions", "instructions",
    "skills", "rag_bases", "connections", "memory", "delegation", "other",
)


# ``t`` and ``st`` for this module itself: the helpers below render and
# localise, so they resolve through the same late layer. ``pin_settings_section``
# is late too: a test may replace it on the facade, and a save handler must
# honour that replacement.
t = late("t")
st = _SHARED_ST
pin_settings_section = late("pin_settings_section")


def pin_settings_section_impl(slug: str, section: str) -> None:
    """Ask the settings page to reopen *section* on the next render pass.

    A save handler calls this before ``st.rerun()``: the page reads the
    request before it creates the tabs and activates the matching one, so a
    save keeps the user in the section they were editing instead of dropping
    them on the first tab.
    """
    if section in SETTINGS_SECTIONS:
        from ui.pages.orchestrator_text import settings_section_key
        st.session_state[settings_section_key(slug)] = section


def settings_save_failed_impl(slug: str, section: str, message_key: str,
                              lang: str) -> None:
    """Report a failed settings save and keep the user in *section*.

    The message is queued and the page is rerun: the next pass paints it
    below the page content, so the error is visible together with the very
    section whose save failed.
    """
    pin_settings_section(slug, section)
    flash_error(t(message_key, lang=lang))
    st.rerun()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
