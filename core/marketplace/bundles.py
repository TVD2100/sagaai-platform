"""
core.marketplace.bundles - curated sets of entities installed as one run.

Two shapes of a bundle are supported:

  * the recommended package of one agent card - the ``recommends`` and
    ``bundle`` lists of its manifest, shown as checkboxes next to it;
  * a catalog-level ``bundles/<id>.json`` record pinning exact versions of
    several entities, so one action reproduces a whole set.

Both end up in the same plan structure, and the plan never touches the
disk: it states what installing each member would do (``install``,
``update``, ``installed``, ``incompatible``, ``unsupported``) so the page
can paint the checkboxes without a single write.

A bundled install is all-or-nothing. Every member is installed through the
regular per-entity installer, and a failure anywhere rolls back the members
this run already put in place, in reverse order. Members that were already
installed before the run are not touched: their registry row predates the
run, and undoing it would delete an installation the user made earlier.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from core.marketplace import installer as installer_mod
from core.marketplace import registry, rollback, source as source_mod, store


class BundleError(store.MarketplaceError):
    """Raised when a bundled install cannot be completed."""


#: Schema id of a catalog-level bundle record.
BUNDLE_SCHEMA = "sagaai.marketplace.bundle/1"

#: Types a card may recommend, in the order a missing type is probed.
_PROBE_TYPES = ("agent", "skill", "rag", "mcp")


def _clean_member(raw) -> Optional[dict]:
    """Normalize one member declaration, or return None when unusable."""
    if isinstance(raw, str) and raw.strip():
        return {"type": "", "id": raw.strip(), "version": "", "default_selected": True}
    if isinstance(raw, dict):
        entity_id = str(raw.get("id") or "").strip()
        if not entity_id:
            return None
        return {
            "type": str(raw.get("type") or "").strip(),
            "id": entity_id,
            "version": str(raw.get("version") or "").strip(),
            "default_selected": bool(raw.get("default_selected", True)),
        }
    return None


def members_of(entity: dict) -> List[dict]:
    """Return the recommended package of a manifest as one merged list.

    ``recommends`` carries the dependency view (possibly with version
    ranges) and ``bundle`` the human-readable view of the same idea. A
    member named by both appears once; a filled field wins over an empty
    one, so the card never shows the same entity twice with two states.
    """
    merged: Dict[str, dict] = {}
    for field in ("recommends", "bundle"):
        raw_list = entity.get(field)
        if not isinstance(raw_list, (list, tuple)):
            continue
        for raw in raw_list:
            member = _clean_member(raw)
            if member is None:
                continue
            current = merged.get(member["id"])
            if current is None:
                merged[member["id"]] = dict(member)
                continue
            if not current.get("type") and member["type"]:
                current["type"] = member["type"]
            if not current.get("version") and member["version"]:
                current["version"] = member["version"]
            # A member selected by either source starts selected.
            current["default_selected"] = bool(
                current.get("default_selected") or member["default_selected"]
            )
    return list(merged.values())


def _member_entry(member: dict) -> dict:
    """Return the plan entry of one member before its manifest is read."""
    return {
        "type": member["type"],
        "id": member["id"],
        "version": member["version"],
        "default_selected": member["default_selected"],
        "name": member["id"],
        "state": "unavailable",
        "installed_version": "",
        "license_required": False,
        "size_bytes": 0,
        "message": "",
    }


def _member_state(member: dict, transport: source_mod.MarketplaceSource) -> dict:
    """Return what installing *member* would do, reading its manifest.

    A member without a declared type is probed across the known types, so a
    hand-written ``recommends: ["rag_base_creator"]`` still resolves. Any
    transport failure stays inside the entry: one missing member must not
    blank the whole package preview.
    """
    entry = _member_entry(member)
    candidates = [member["type"]] if member["type"] else list(_PROBE_TYPES)
    last_error = ""
    for candidate in candidates:
        try:
            entity = transport.fetch_entity(
                candidate, member["id"], member["version"] or None
            )
        except Exception as exc:
            last_error = str(getattr(exc, "message", exc) or exc)
            continue

        entry["type"] = candidate
        entry["name"] = installer_mod._display_name(entity) or member["id"]
        entry["version"] = str(entity.get("version") or member["version"])
        package = entity.get("package") if isinstance(entity.get("package"), dict) else {}
        entry["size_bytes"] = int(package.get("size") or 0)
        licence = entity.get("license") if isinstance(entity.get("license"), dict) else {}
        entry["license_required"] = bool(licence.get("acceptance_required"))

        if candidate not in installer_mod.SUPPORTED_TYPES:
            entry["state"] = "unsupported"
            entry["message"] = f"type '{candidate}' cannot be installed yet"
            return entry

        row = registry.get_installed(candidate, member["id"])
        entry["installed_version"] = str(row.get("version") or "") if row else ""
        if row is None:
            entry["state"] = "install"
        elif entry["installed_version"] == entry["version"]:
            entry["state"] = "installed"
        else:
            entry["state"] = "update"

        compatible, reason = installer_mod._platform_compatible(entity)
        if not compatible:
            entry["state"] = "incompatible"
            entry["message"] = reason
        return entry

    entry["message"] = last_error or "not found in the catalog"
    return entry


def plan(
    entity: dict,
    source: Optional[source_mod.MarketplaceSource] = None,
) -> dict:
    """Return the recommended-package plan of one manifest.

    The plan is read-only: every member's manifest is fetched (lazily, the
    transport caches them) and compared with the registry. The result
    carries ``items`` (one entry per member) plus the counters the page
    needs to decide whether to enable the install button.
    """
    transport = source or source_mod.default_source()
    items = [_member_state(member, transport) for member in members_of(entity)]
    actionable = ("install", "update")
    return {
        "ok": True,
        "items": items,
        "total": len(items),
        "installable": sum(1 for item in items if item["state"] in actionable),
        "updates": sum(1 for item in items if item["state"] == "update"),
        "selected": sum(
            1 for item in items
            if item["default_selected"] and item["state"] in actionable
        ),
        "needs_license": any(
            item["license_required"] for item in items if item["state"] in actionable
        ),
        "size_bytes": sum(
            item["size_bytes"] for item in items if item["state"] in actionable
        ),
    }


def _chosen(items: Sequence[dict], selected: Optional[Sequence[dict]]) -> List[dict]:
    """Return the plan entries the caller asked to install.

    Without an explicit selection the package default applies. Members that
    are already installed or cannot be installed are dropped here, so the
    install loop only ever sees work that changes something.
    """
    actionable = ("install", "update")
    if selected is None:
        return [
            item for item in items
            if item["default_selected"] and item["state"] in actionable
        ]
    wanted = set()
    for raw in selected:
        if isinstance(raw, dict):
            wanted.add((str(raw.get("type") or ""), str(raw.get("id") or "")))
        elif isinstance(raw, str) and raw.strip():
            wanted.add(("", raw.strip()))
    return [
        item for item in items
        if item["state"] in actionable
        and ((item["type"], item["id"]) in wanted or ("", item["id"]) in wanted)
    ]


def install(
    entity: dict,
    *,
    channel: str,
    selected: Optional[Sequence[dict]] = None,
    source: Optional[source_mod.MarketplaceSource] = None,
    allow_code: Optional[bool] = None,
    accept_license: bool = False,
    bundle_id: str = "",
) -> dict:
    """Install the chosen recommended-package members as one run.

    Members go through :func:`core.marketplace.installer.install_entity`,
    each taking and releasing the install mutex itself. A bundle is not
    atomic against a concurrent run for that reason, but it is all-or-
    nothing against its own failures: anything this run installed is rolled
    back before the error reaches the caller.

    Parameters:
        entity: the manifest the package belongs to (an agent card).
        channel: release channel recorded on every member row.
        selected: members to install; ``None`` follows ``default_selected``.
        source: catalog transport, injectable for tests.
        allow_code: forwarded to the per-entity installer.
        accept_license: explicit licence acceptance for the whole set.
        bundle_id: identifier stamped on every member row and run record;
            it defaults to the id of the entity that carries the package.
    """
    transport = source or source_mod.default_source()
    group = str(bundle_id or entity.get("id") or "")
    plan_data = plan(entity, transport)
    chosen = _chosen(plan_data["items"], selected)
    if not chosen:
        return {
            "ok": True,
            "noop": True,
            "bundle_id": group,
            "group_id": "",
            "installed": [],
            "plan": plan_data,
        }

    group_id = store.new_install_id()
    done: List[dict] = []
    for item in chosen:
        try:
            member_entity = dict(
                transport.fetch_entity(item["type"], item["id"], item["version"] or None)
            )
        except Exception as exc:
            restored = _rollback_members(done, reason=f"bundle member unavailable: {exc}")
            raise BundleError(
                "bundle_member_unavailable",
                f"Cannot read the manifest of {item['type']}/{item['id']}: {exc}",
                {"bundle_id": group, "member": item["id"], "rolled_back": restored},
            ) from exc
        member_entity["bundle_id"] = group
        try:
            report = installer_mod.install_entity(
                member_entity,
                channel=channel,
                allow_code=allow_code,
                source=transport,
                accept_license=accept_license,
            )
        except Exception as exc:
            # The failed member rolls itself back; the ones before it are
            # undone here so the user never gets half a set.
            restored = _rollback_members(done, reason=f"bundle failed: {exc}")
            raise BundleError(
                "bundle_install_failed",
                f"Installing {item['type']}/{item['id']} failed: "
                f"{getattr(exc, 'message', exc)}",
                {
                    "bundle_id": group,
                    "group_id": group_id,
                    "member": {"type": item["type"], "id": item["id"]},
                    "installed": [entry["id"] for entry in done],
                    "rolled_back": restored,
                },
            ) from exc

        if report.get("noop"):
            # An earlier installation, not this run's work: rolling it back
            # later would delete something the user installed before.
            continue
        record = {
            "type": item["type"],
            "id": item["id"],
            "install_id": str(report.get("install_id") or ""),
            "version": str(report.get("version") or ""),
            "runtime_slug": str(report.get("runtime_slug") or ""),
        }
        # A connector member installs as a connection without credentials:
        # the caller has to tell the user where to fill them in, which it
        # cannot guess once the member report is reduced to its ids.
        secrets = [
            str(name) for name in (report.get("requires_secrets") or [])
            if str(name).strip()
        ]
        if secrets:
            record["requires_secrets"] = secrets
            record["credentials_status"] = str(
                report.get("credentials_status") or "pending"
            )
        done.append(record)
        if record["install_id"]:
            try:
                store.update_run(
                    record["install_id"],
                    group_id=group_id,
                    bundle_id=group,
                )
            except Exception:
                pass

    return {
        "ok": True,
        "noop": False,
        "bundle_id": group,
        "group_id": group_id,
        "installed": done,
        "plan": plan_data,
    }


def _rollback_members(done: Sequence[dict], *, reason: str) -> List[dict]:
    """Undo the members of a bundle run in reverse order.

    Every member is attempted even when an earlier rollback failed: a
    half-undone set is worse than a fully attempted one, and the caller
    surfaces whatever could not be restored.
    """
    report: List[dict] = []
    for entry in reversed(list(done)):
        install_id = str(entry.get("install_id") or "")
        if not install_id:
            continue
        try:
            result = rollback.rollback_run(install_id, reason=reason)
            report.append({
                "type": entry.get("type"),
                "id": entry.get("id"),
                "ok": True,
                "restored_files": list(result.get("restored_files") or []),
                "unrestored": list(result.get("unrestored") or []),
            })
        except Exception as exc:
            report.append({
                "type": entry.get("type"),
                "id": entry.get("id"),
                "ok": False,
                "message": str(exc),
            })
    return report


def rollback_group(group_id: str, *, reason: str = "user") -> dict:
    """Undo every installed member of one bundle run.

    A bundled install is undone as a whole, never member by member: the set
    was installed as one action, and a partial undo would leave the
    ``rag_bases`` / tool bindings of the surviving members pointing at
    entities that are gone.
    """
    wanted = str(group_id or "").strip()
    if not wanted:
        raise BundleError("invalid_group", "Bundle group id is empty")
    runs = [
        record for record in store.load_history()
        if str(record.get("group_id") or "") == wanted
        and str(record.get("status") or "") == "done"
    ]
    runs.sort(key=lambda record: str(record.get("started_at") or ""), reverse=True)
    members = [
        {
            "type": record.get("type"),
            "id": record.get("id"),
            "install_id": record.get("install_id"),
        }
        for record in runs
    ]
    return {
        "ok": True,
        "group_id": wanted,
        "members": _rollback_members(members, reason=reason),
    }


def catalog_bundles(
    source: Optional[source_mod.MarketplaceSource] = None,
    force: bool = False,
) -> List[dict]:
    """Return the bundles listed by the catalog index.

    The index carries only ``{id, name, path}`` per bundle; the items are
    read when the user actually opens one, so a page render never downloads
    every bundle record.
    """
    transport = source or source_mod.default_source()
    try:
        index = transport.fetch_index(force=force)
    except Exception:
        return []
    bundles = index.get("bundles") if isinstance(index, dict) else None
    if not isinstance(bundles, (list, tuple)):
        return []
    result: List[dict] = []
    for raw in bundles:
        if isinstance(raw, str) and raw.strip():
            result.append({"id": raw.strip(), "name": raw.strip(), "path": ""})
        elif isinstance(raw, dict) and str(raw.get("id") or "").strip():
            result.append({
                "id": str(raw["id"]).strip(),
                "name": str(raw.get("name") or raw["id"]).strip(),
                "path": str(raw.get("path") or "").strip(),
            })
    return result


def load_bundle(
    bundle_id: str,
    source: Optional[source_mod.MarketplaceSource] = None,
) -> dict:
    """Fetch one catalog bundle record and validate its member list."""
    transport = source or source_mod.default_source()
    record = transport.fetch_bundle(bundle_id)
    items = []
    for raw in record.get("items") or []:
        member = _clean_member(raw)
        if member is not None:
            items.append(member)
    if not items:
        raise BundleError(
            "empty_bundle",
            f"Bundle '{bundle_id}' declares no usable items",
            {"id": bundle_id},
        )
    return {
        "id": str(record.get("id") or bundle_id),
        "name": str(record.get("name") or bundle_id),
        "version": str(record.get("version") or ""),
        "items": items,
    }


def plan_bundle(
    bundle_id: str,
    source: Optional[source_mod.MarketplaceSource] = None,
) -> dict:
    """Return the plan of a catalog-level bundle.

    The bundle record is turned into the same ``recommends`` shape a card
    uses, so one planner serves both entry points.
    """
    record = load_bundle(bundle_id, source)
    carrier = {"id": record["id"], "bundle": record["items"]}
    plan_data = plan(carrier, source)
    plan_data["bundle"] = {"id": record["id"], "name": record["name"], "version": record["version"]}
    return plan_data
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
