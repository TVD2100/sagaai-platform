"""
core.marketplace.mcp_removal - what an MCP uninstall takes off the disk.

An MCP entity owns no folder. Its installation is a preset file under
``DATA_DIR/connector_presets/<service>``; the connections the user created
from that preset are separate records with their own credentials and are
never touched by an uninstall.

A registry row written before presets were installed records the id of a
connection the old installer created. Such a row is still handled here, so
an upgrade does not leave a credential-less draft behind on the
Connections tab - one the user has configured is kept, because it may hold
credentials entered by hand and be used by prompts that have nothing to do
with the showcase entry.
"""
from __future__ import annotations

#: Connector service MCP entities belong to.
SERVICE = "mcp"


def remove_preset(type_: str, runtime_slug: str) -> bool:
    """Remove the preset an MCP entity installed into the user root.

    Removing it takes the tile off the MCP tab and leaves the bundled
    template of the same id - if any - visible again. A bundled preset is
    the platform's own file and is never deleted: the uninstall only drops
    the registry row and the removal marker.

    Returns True when a preset file was removed.
    """
    if type_ != SERVICE or not runtime_slug:
        return False
    try:
        from core import connector_presets

        if connector_presets.bundled_preset(runtime_slug, SERVICE):
            return False
        return connector_presets.delete_preset(runtime_slug, SERVICE)
    except Exception:
        return False


def remove_connection(type_: str, runtime_slug: str) -> bool:
    """Remove the connection an older MCP install created, when it is safe to.

    A row written before presets were installed records a connection id in
    ``runtime_slug``. That draft is removed on uninstall so the server does
    not stay in the list the user just left; a connection with credentials
    is kept.

    Returns True when a connection was removed.
    """
    if type_ != SERVICE or not runtime_slug:
        return False
    try:
        from core import connectors

        manifest = connectors.get_connection(runtime_slug) or {}
        if not manifest:
            return False
        if str(manifest.get("credentials_status") or "") == "active":
            return False
        connectors.delete_connection(runtime_slug)
        return True
    except Exception:
        return False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
