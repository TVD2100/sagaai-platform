"""
core.marketplace.agent_installer - public entry point of an ``agent`` install.

An agent package ships a folder: ``orchestrator.json``, ``system_prompt.md``,
``instructions/*.md`` and ``functions/*.py`` for an orchestrator, or
``manifest.json``, ``prompt.md`` and ``files/*`` for a helper. Installing it
means three things happen in a fixed order (D-03, D-07):

  1. the package is downloaded, hash-verified and unpacked into staging;
  2. ``config_defaults`` of the manifest are merged into the bundle config
     BEFORE the import - the importer writes the config it receives, so a
     merge after the fact would either be lost or overwrite user settings;
  3. the folder and the DB row are created by the importer, and the registry
     row is written last as the commit marker.

The file manifest stored in the registry is hashed from the LIVE folder after
the import, not from the package: the importer regenerates
``orchestrator.json`` and ``system_prompt.md`` from the database, so the
package bytes are not what ends up on disk. The hashing area (D-04) is the
agent part of the folder - ``orchestrator.json``, ``system_prompt.md``,
``instructions/*.md`` and ``functions/*.py`` - which keeps a fresh
installation clean while a note the user drops next to it stays theirs.

This module is the facade of the agent install. The steps live in:

  * :mod:`core.marketplace.agent_preflight` - validation and folder naming;
  * :mod:`core.marketplace.agent_orchestrator` - installing an employee;
  * :mod:`core.marketplace.agent_assistant` - installing a helper;
  * :mod:`core.marketplace.agent_areas` - which files belong to an agent;
  * :mod:`core.marketplace.agent_package` - reading a package, merging updates;
  * :mod:`core.marketplace.agent_run` - the run ledger and its rollback.

Every name the package had before the split is re-exported here, so existing
``from core.marketplace.agent_installer import ...`` keeps working.
"""
from __future__ import annotations

from typing import Optional

from core.marketplace import source as source_mod
from core.marketplace.agent_areas import (  # noqa: F401
    _assistant_area_files,
    _orchestrator_area_files,
    _remove_stale_area_files,
    area_files,
)
from core.marketplace.agent_assistant import _install_assistant  # noqa: F401
from core.marketplace.agent_orchestrator import _install_orchestrator  # noqa: F401
from core.marketplace.agent_package import (  # noqa: F401
    _apply_config_defaults,
    _assistant_bundle_from_folder,
    _bundle_from_folder,
    _read_json,
    _read_text,
)
from core.marketplace.agent_preflight import (  # noqa: F401
    SUPPORTED_KINDS,
    _agent_kind,
    _install_preflight,
    _pick_runtime_slug,
    _resolve_slug,
    _slug_taken,
)
from core.marketplace.agent_run import fail_agent_run as _fail_run  # noqa: F401
from core.marketplace.installer import InstallError


def install_agent(
    entity: dict,
    *,
    channel: str,
    allow_code: Optional[bool] = None,
    source: Optional[source_mod.MarketplaceSource] = None,
    accept_license: bool = False,
    runtime_slug: Optional[str] = None,
) -> dict:
    """Install one agent of any supported kind and return a report.

    The kind decides where the folder lives and which importer turns it into
    a database row; everything that can refuse the request - the marketplace
    switch, the platform bounds, the licence, the code policy - is checked
    before the download by :func:`_install_preflight`.

    Parameters:
        entity: parsed ``entity.json`` of the agent.
        channel: release channel name recorded on the registry row.
        allow_code: install executable files; ``None`` reads the setting.
        source: catalog transport; ``None`` uses the configured catalog.
        accept_license: explicit licence acceptance for entities that
            require it.
        runtime_slug: folder name to use; ``None`` derives it from the
            manifest and appends a suffix when it is taken.

    Re-installing the same version is a no-op. Any failure after the first
    change rolls the run back, so a half-installed agent is never left
    behind.
    """
    if not isinstance(entity, dict) or not entity.get("id"):
        raise InstallError("invalid_entity", "Entity manifest has no id")
    kind = _agent_kind(entity)
    preflight = _install_preflight(
        entity,
        allow_code=allow_code,
        source=source,
        accept_license=accept_license,
    )
    noop = preflight.get("noop")
    if noop is not None:
        return noop
    if kind == "assistant":
        return _install_assistant(
            entity, preflight, channel=channel, runtime_slug=runtime_slug
        )
    return _install_orchestrator(
        entity, preflight, channel=channel, runtime_slug=runtime_slug
    )


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
