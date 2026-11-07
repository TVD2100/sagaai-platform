# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_state - Session-state helpers and the economy cache of the orchestrator page.

Part of the orchestrator page. A name the tests may replace through the
facade (``ui.pages.orchestrator``) is resolved late, at call time, so a
patch installed on the facade reaches the implementation here.
"""
from typing import Dict

from dev_agent.agent_loop import (
    apply_economy_cache,
    economy_cache_to_dict,
)
from ui.pages.orchestrator_shared import late, st_proxy
from ui.pages.orchestrator_text import (
    _chat_pref_config_keys,
    _sk,
    build_state_keys,
)

st = st_proxy()
_set_ss = late("_set_ss")
_ss = late("_ss")
get_orchestrator = late("get_orchestrator")
save_orchestrator = late("save_orchestrator")


CHAT_PAGE_SIZE = 50


ECONOMY_MODE_ALWAYS_ON = True


def _chat_prefs(slug: str) -> Dict[str, bool]:
    orch = get_orchestrator(slug)
    cfg = orch.get("config", {}) if orch else {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {key: bool(cfg.get(conf_key, default)) for key, (conf_key, default) in _chat_pref_config_keys().items()}


def _save_chat_pref(slug: str, key: str, value: bool) -> bool:
    mapping = _chat_pref_config_keys()
    if key not in mapping:
        return False
    conf_key, _ = mapping[key]
    orch = get_orchestrator(slug)
    if orch is None:
        return False
    cfg = orch.get("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = dict(cfg)
    cfg[conf_key] = bool(value)
    return save_orchestrator(slug, config=cfg)


def _make_state_keys(slug: str) -> dict:
    """Return a dict of session-state key -> default for this orchestrator."""
    return build_state_keys(slug, _chat_prefs(slug), ECONOMY_MODE_ALWAYS_ON, CHAT_PAGE_SIZE)


def _init_orch_state_impl(slug: str) -> None:
    """Initialise session_state keys for the given orchestrator slug."""
    for k, v in _make_state_keys(slug).items():
        if k not in st.session_state:
            st.session_state[k] = v


def _ss_impl(slug: str, key: str):
    """Get a slug-prefixed session-state value."""
    return st.session_state.get(_sk(slug, key))


def _set_ss_impl(slug: str, key: str, value):
    st.session_state[_sk(slug, key)] = value


def _pop_ss(slug: str, key: str):
    return st.session_state.pop(_sk(slug, key), None)


def _save_economy_cache(slug: str, state) -> None:
    """Persist the cache-friendly economy window parameters.

    The loop state itself gets cleared for terminal statuses (done, applied,
    discarded, error, ...), but the anchor must survive so the next turn
    continues the accumulation window instead of restarting from the
    bare tail.

    A state whose anchor was never established (None) carries no
    accumulation to preserve; skipping the save leaves an existing
    persisted anchor intact instead of clobbering it.
    """
    if state is None:
        return
    if getattr(state, "economy_anchor", None) is None:
        return
    _set_ss(slug, "economy_cache", economy_cache_to_dict(state))


def _load_economy_cache(slug: str, state) -> None:
    """Restore persisted economy window parameters onto a fresh loop state."""
    cache = _ss(slug, "economy_cache")
    if isinstance(cache, dict):
        apply_economy_cache(cache, state)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
