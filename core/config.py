"""
core.config - application configuration via SQLite.
Public API mirrors the monolith: load_config, save_config, has_key.
Also provides DevAgent-specific config helpers (stored in the same KV table).

Secrets (API keys for configured services) are encrypted before entering the DB
and decrypted on load. Environment variables of the form
    SAGAAI_<SERVICE_NAME>_KEY
    SAGAAI_<SERVICE_NAME>_KEY2
take precedence over the stored values.

DevAgent settings (load_devagent_config / save_devagent_config) are now proxied
through core.orchestrators for backward compatibility.

Default values are read from the bundled defaults/ folder:
  - defaults/settings/global.json                  - global defaults
  - orchestrators/dev_agent/orchestrator.json      - canonical DevAgent bundle
  - built-in dict                                  - final fallback

The DevAgent system prompt lives in dev_agent/system_prompt.md and is managed
by core.orchestrators.ensure_builtin_orchestrators, not here.
"""
import os
import json
from pathlib import Path
from typing import Optional

from cryptography.fernet import InvalidToken

from storage.repository import repo_load_config, repo_save_config
from core.crypto import encrypt, decrypt, is_secret_key


def _secret_keys() -> set:
    """Return the set of DB key names that are considered secrets
    for the currently registered services."""
    # Import here to avoid circular dependency at module level
    from core.services import get_services
    services = get_services()
    secrets = set()
    for svc in services.values():
        for field in ("config_key", "config_key2"):
            val = svc.get(field, "")
            if val:
                secrets.add(val)
    return secrets


def load_config() -> dict:
    """Return the full configuration as a dict.

    1. Load raw values from the DB.
    2. Decrypt known secret keys.  If decryption fails the value is
       replaced with an empty string so callers never receive a token
       they cannot use.
    3. Overlay environment variables (they win).
    """
    config = repo_load_config()

    # --- decrypt stored secrets ------------------------------------------------
    secrets = _secret_keys()
    for key in list(config.keys()):
        if key in secrets and isinstance(config[key], str):
            try:
                config[key] = decrypt(config[key])
            except InvalidToken:
                config[key] = ""

    # --- overlay environment variables -----------------------------------------
    _merge_env_keys(config)

    return config


def save_config(config: dict) -> bool:
    """Persist the configuration dict. Secret values are encrypted first."""
    secrets = _secret_keys()
    safe = {}
    for key, val in config.items():
        if key in secrets and isinstance(val, str) and val:
            safe[key] = encrypt(val)
        else:
            safe[key] = val
    return repo_save_config(safe)


def has_key(service_def: dict) -> bool:
    """Return True if the API key for *service_def* is configured and non-empty."""
    cfg  = load_config()
    key1 = cfg.get(service_def.get("config_key", ""), "")
    if isinstance(key1, str):
        return bool(key1.strip())
    return bool(key1)


# ─── Environment variable helpers ─────────────────────────────────────────────

_IS_ENV_KEY_CACHE: dict | None = None


def _env_key_for_service(svc_name: str, config_key_field: str) -> str:
    """Construct the expected environment variable name for a service's API key.
    e.g. ("deepseek", "config_key") -> "SAGAAI_DEEPSEEK_KEY"
         ("deepseek", "config_key2") -> "SAGAAI_DEEPSEEK_KEY2"
    """
    suffix = "" if config_key_field == "config_key" else "2"
    return f"SAGAAI_{svc_name.upper()}_KEY{suffix}"


def is_env_key_set_for_service(svc_name: str, config_key_field: str) -> bool:
    """Return True if the environment variable for this service and key field
    (config_key or config_key2) is set and non-empty.
    """
    env_var = _env_key_for_service(svc_name, config_key_field)
    return bool(os.environ.get(env_var, "").strip())


def list_env_keys() -> dict:
    """Return a dict suitable for display in the settings UI.
    {
        "deepseek": {
            "env_keys": [
                {"var": "SAGAAI_DEEPSEEK_KEY", "set": True},
                {"var": "SAGAAI_DEEPSEEK_KEY2", "set": False},  // if config_key2 is defined
            ],
            "db_value_masked": "***" if a secret DB value exists else "",
            "env_wins": True,   // at least one env var is set
        },
        ...
    }
    """
    from core.services import get_services
    services = get_services()
    result = {}
    # Load raw config (without env overlay) so we can show DB vs env
    raw_cfg = repo_load_config()
    secrets = _secret_keys()

    for svc_name, svc in services.items():
        info = {"env_keys": [], "db_value_masked": "", "env_wins": False}
        for field in ("config_key", "config_key2"):
            db_key = svc.get(field, "")
            if not db_key:
                continue
            env_var = _env_key_for_service(svc_name, field)
            is_set = bool(os.environ.get(env_var, "").strip())
            info["env_keys"].append({"var": env_var, "set": is_set})
            if is_set:
                info["env_wins"] = True
        # Show whether a DB value exists (masked)
        db_key_name = svc.get("config_key", "")
        if db_key_name:
            raw_val = raw_cfg.get(db_key_name, "")
            if isinstance(raw_val, str) and raw_val:
                try:
                    plain = decrypt(raw_val) if db_key_name in secrets else raw_val
                    if plain:
                        info["db_value_masked"] = "***"
                except InvalidToken:
                    info["db_value_masked"] = "*** (decryption error)"
        result[svc_name] = info
    return result


def _merge_env_keys(config: dict) -> None:
    """Overlay environment variables onto *config* in place."""
    from core.services import get_services
    services = get_services()
    for svc_name, svc in services.items():
        for field in ("config_key", "config_key2"):
            db_key = svc.get(field, "")
            if not db_key:
                continue
            env_var = _env_key_for_service(svc_name, field)
            env_val = os.environ.get(env_var, "").strip()
            if env_val:
                config[db_key] = env_val


# ─── DevAgent config helpers ──────────────────────────────────────────────────
# These are now thin proxies over core.orchestrators.  They exist solely for
# backward compatibility with code that imports load_devagent_config /
# save_devagent_config from core.config.

DEVAGENT_PREFIX = "devagent."

# Path to the canonical DevAgent orchestrator bundle. The "config" section of
# orchestrators/dev_agent/orchestrator.json is the single source of truth for
# DevAgent's default settings (with the built-in fallback below when the bundle
# is missing, e.g. on a clean install before first boot). The DevAgent system
# prompt lives in dev_agent/system_prompt.md and is managed by core.orchestrators.
_DEFAULT_DEVAGENT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "orchestrators"
    / "dev_agent"
    / "orchestrator.json"
)

# Built-in fallback defaults (used only when the canonical bundle is missing).
_DEVAGENT_FALLBACK_DEFAULTS = {
    "service": "",
    "model": "",
    "temperature": "0.2",
    "prompt_text": "",
    "strong_service": "DeepSeek",
    "strong_model": "deepseek-v4-pro",
    "strong_temperature": "0.4",
    "strong_max_tokens": "384000",
    "strong_reasoning_effort": "max",
    "weak_service": "DeepSeek",
    "weak_model": "deepseek-v4-pro",
    "weak_temperature": "0.4",
    "weak_max_tokens": "384000",
    "weak_reasoning_effort": "max",
    "search_service": "YandexAI",
    "search_model": "aliceai-llm-flash",
    "search_temperature": "0.3",
    "search_max_tool_calls": "1",
    "search_reasoning_effort": "high",
    "web_search_prompt": "",
    "economy_tail_messages": "30",
    "economy_cache_enabled": "true",
    "economy_cache_multiplier": "3",
    "enabled_skills": "[]",
    "enabled_connections": "[]",
}


def _merge_defaults_overrides(data: dict) -> Optional[dict]:
    """Return a defaults dict merged over the built-in fallback, or None.

    Values of *data* replace fallback values for keys that exist in both.
    """
    if not isinstance(data, dict):
        return None
    merged = dict(_DEVAGENT_FALLBACK_DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in merged})
    return merged


def _load_devagent_defaults_from_bundle() -> dict:
    """Read default DevAgent configuration from the canonical orchestrator bundle.

    Preference order:
      1. config section of orchestrators/dev_agent/orchestrator.json
      2. built-in fallback defaults

    Returns a dict with the same shape as _DEVAGENT_FALLBACK_DEFAULTS.
    """
    path = _DEFAULT_DEVAGENT_CONFIG_PATH
    try:
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
            if raw.startswith("\ufeff"):
                raw = raw[1:]
            bundle = json.loads(raw)
            merged = _merge_defaults_overrides(bundle.get("config", {}))
            if merged is not None:
                return merged
    except Exception:
        pass
    return dict(_DEVAGENT_FALLBACK_DEFAULTS)


_devagent_defaults_cache: dict | None = None


def _get_devagent_defaults() -> dict:
    """Return the effective DevAgent defaults (canonical bundle → cache → fallback)."""
    global _devagent_defaults_cache
    if _devagent_defaults_cache is None:
        _devagent_defaults_cache = _load_devagent_defaults_from_bundle()
    return _devagent_defaults_cache


def get_devagent_defaults() -> dict:
    """Return the effective DevAgent defaults (public API).

    Values are read from the canonical orchestrator bundle
    (orchestrators/dev_agent/orchestrator.json, config section) and merged
    over the built-in fallback defaults. The result is a flat dict of
    string values compatible with the legacy config contract.
    """
    return _get_devagent_defaults()


def reload_devagent_defaults() -> None:
    """Clear cached defaults so the next call re-reads the canonical bundle."""
    global _devagent_defaults_cache
    _devagent_defaults_cache = None


def get_default_economy_tail_messages() -> int:
    """Return the default number of recent messages used in economy mode.

    This value is read from the canonical DevAgent orchestrator bundle
    (orchestrators/dev_agent/orchestrator.json) with the built-in values as
    fallback. It is the single source of the default tail length.
    """
    defaults = _get_devagent_defaults()
    try:
        return int(defaults.get("economy_tail_messages", 30))
    except Exception:
        return 30


def get_default_economy_cache_enabled() -> bool:
    """Return whether cache-friendly economy mode is enabled by default."""
    defaults = _get_devagent_defaults()
    raw = defaults.get("economy_cache_enabled", "true")
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def get_default_economy_cache_multiplier() -> int:
    """Return the default cache-window multiplier (xN)."""
    defaults = _get_devagent_defaults()
    try:
        return max(1, int(defaults.get("economy_cache_multiplier", 3)))
    except Exception:
        return 3


def get_default_strong_max_tokens() -> int:
    """Return the default max output tokens for the strong model."""
    defaults = _get_devagent_defaults()
    try:
        return max(0, int(defaults.get("strong_max_tokens", 384000)))
    except Exception:
        return 384000


def get_default_weak_max_tokens() -> int:
    """Return the default max output tokens for the weak model."""
    defaults = _get_devagent_defaults()
    try:
        return max(0, int(defaults.get("weak_max_tokens", 384000)))
    except Exception:
        return 384000


# ─── Global defaults (defaults/settings/global.json) ──────────────────────────

_global_defaults_cache: dict | None = None


def _get_global_defaults() -> dict:
    """Return the contents of defaults/settings/global.json (cached)."""
    global _global_defaults_cache
    if _global_defaults_cache is None:
        try:
            from core import defaults as defaults_mod
            _global_defaults_cache = defaults_mod.load_global_settings()
        except Exception:
            _global_defaults_cache = {}
    return _global_defaults_cache


def reload_global_defaults() -> None:
    """Clear cached global defaults so the next call re-reads the JSON file."""
    global _global_defaults_cache
    _global_defaults_cache = None


def get_default_ui_lang() -> str:
    """Return the default UI language name from defaults/settings/global.json."""
    raw = _get_global_defaults().get("ui_lang", "")
    return raw.strip() if isinstance(raw, str) and raw.strip() else ""


def get_default_providers_preset() -> str:
    """Return the default providers preset key from defaults/settings/global.json."""
    raw = _get_global_defaults().get("providers_preset", "")
    return raw.strip() if isinstance(raw, str) and raw.strip() else ""


def load_devagent_config() -> dict:
    """Return DevAgent settings dict.

    Delegates to ``core.orchestrators.load_devagent_config('dev_agent')``.
    Falls back to JSON file / hardcoded defaults if the orchestrator table
    is not yet populated (e.g. during bootstrap).
    """
    try:
        from core.orchestrators import load_devagent_config as _orch_load
        cfg = _orch_load()
        # Always return string values for backward compat.
        return {k: str(v) if not isinstance(v, str) else v for k, v in cfg.items()}
    except Exception:
        # Orchestrator table not available yet (initial bootstrap) -
        # fall back to the old KV-driven logic.
        cfg = load_config()
        defaults = _get_devagent_defaults()
        result: dict = {}
        for key, fallback in defaults.items():
            result[key] = cfg.get(f"{DEVAGENT_PREFIX}{key}", fallback)
        return result


def save_devagent_config(service: str, model: str, temperature: float,
                         prompt_text: str,
                         strong_service: str = "", strong_model: str = "",
                         strong_temperature: float = 0.2,
                         weak_service: str = "", weak_model: str = "",
                         weak_temperature: float = 0.5,
                         search_service: str = "", search_model: str = "",
                         search_temperature: float = 0.3,
                         search_max_tool_calls: int = 3,
                         economy_tail_messages: Optional[int] = None,
                         economy_cache_enabled: Optional[bool] = None,
                         economy_cache_multiplier: Optional[int] = None) -> bool:
    """Persist DevAgent settings.

    Delegates to ``core.orchestrators.save_devagent_config(...)``.
    Falls back to KV-store if orchestrator table is unavailable.
    """
    try:
        from core.orchestrators import save_devagent_config as _orch_save
        return _orch_save(
            service=service, model=model, temperature=temperature,
            prompt_text=prompt_text,
            strong_service=strong_service, strong_model=strong_model,
            strong_temperature=strong_temperature,
            weak_service=weak_service, weak_model=weak_model,
            weak_temperature=weak_temperature,
            search_service=search_service, search_model=search_model,
            search_temperature=search_temperature,
            search_max_tool_calls=search_max_tool_calls,
            economy_tail_messages=economy_tail_messages,
            economy_cache_enabled=economy_cache_enabled,
            economy_cache_multiplier=economy_cache_multiplier,
        )
    except Exception:
        # Fallback: write to KV store.
        cfg = load_config()
        cfg[f"{DEVAGENT_PREFIX}service"] = service
        cfg[f"{DEVAGENT_PREFIX}model"] = model
        cfg[f"{DEVAGENT_PREFIX}temperature"] = str(temperature)
        cfg[f"{DEVAGENT_PREFIX}prompt_text"] = prompt_text
        cfg[f"{DEVAGENT_PREFIX}strong_service"] = strong_service
        cfg[f"{DEVAGENT_PREFIX}strong_model"] = strong_model
        cfg[f"{DEVAGENT_PREFIX}strong_temperature"] = str(strong_temperature)
        cfg[f"{DEVAGENT_PREFIX}strong_max_tokens"] = str(get_default_strong_max_tokens())
        cfg[f"{DEVAGENT_PREFIX}weak_service"] = weak_service
        cfg[f"{DEVAGENT_PREFIX}weak_model"] = weak_model
        cfg[f"{DEVAGENT_PREFIX}weak_temperature"] = str(weak_temperature)
        cfg[f"{DEVAGENT_PREFIX}weak_max_tokens"] = str(get_default_weak_max_tokens())
        cfg[f"{DEVAGENT_PREFIX}search_service"] = search_service
        cfg[f"{DEVAGENT_PREFIX}search_model"] = search_model
        cfg[f"{DEVAGENT_PREFIX}search_temperature"] = str(search_temperature)
        cfg[f"{DEVAGENT_PREFIX}search_max_tool_calls"] = str(search_max_tool_calls)
        tail = economy_tail_messages if economy_tail_messages is not None else get_default_economy_tail_messages()
        cfg[f"{DEVAGENT_PREFIX}economy_tail_messages"] = str(tail)
        cfg[f"{DEVAGENT_PREFIX}economy_cache_enabled"] = (
            str(bool(economy_cache_enabled)).lower()
            if economy_cache_enabled is not None
            else str(get_default_economy_cache_enabled()).lower()
        )
        cfg[f"{DEVAGENT_PREFIX}economy_cache_multiplier"] = str(
            max(1, int(economy_cache_multiplier))
            if economy_cache_multiplier is not None
            else get_default_economy_cache_multiplier()
        )
        return save_config(cfg)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
