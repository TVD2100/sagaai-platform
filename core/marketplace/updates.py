"""
core.marketplace.updates - what changed in the catalog since the install.

Three user-facing questions live here:

  * "is there a newer version of what I have?" - ``check_entity_updates()``;
  * "did I change anything myself?" - ``verify_installed()``, built on the
    same comparison the conflict matrix uses instead of a second detector;
  * "then update it" - ``apply_entity_updates()``.

The check is the only marketplace operation that contacts the catalog on
the user's behalf, and it is never called on start-up: the section shows
"press to check" until somebody presses it. ``tests/test_marketplace_no_network.py``
enforces that at the AST level rather than by convention.

Version arithmetic follows the install matrix:

  * a higher catalog version is an update, unless the platform bound no
    longer matches - then the entry says which platform is required
    instead of offering an install that would fail halfway;
  * the same version with a newer ``updated_at`` is a metadata refresh:
    nothing is downloaded and no file is touched;
  * a higher version whose declared files hash to the installed tree is a
    re-stamp, not an update - the version is recorded and the entry is not
    shown (D-22);
  * pinned installations and entities the user removed are not checked.

Applying an update reuses ``conflicts.resolve_three_way()``: the same
backup, run metadata and rollback path as a conflict resolution. There is
deliberately no second file-moving implementation here.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence

from core.marketplace import config as marketplace_config
from core.marketplace import conflicts, installer, registry, semver, source as source_mod, store


#: Update policies accepted by :func:`apply_entity_updates`.
POLICIES = ("keep_mine", "take_catalog", "skip")

#: Entity types the update check covers. All four are installable now;
#: a type the catalog does not know simply lands in ``unavailable``.
CHECKED_TYPES: tuple = registry.ENTITY_TYPES


class UpdateError(store.MarketplaceError):
    """Raised when updates cannot be checked or applied."""


# ─── Helpers ──────────────────────────────────────────────────────────────

def _installed_subset(entity: dict, skipped: Sequence[str]) -> Dict[str, str]:
    """Return the files an install of *entity* would actually place on disk.

    ``skipped`` holds the code files a strict-mode install left out. The
    tree hash of such an installation covers the same subset, so the
    re-stamp comparison has to build that subset instead of the full
    manifest - otherwise every strict-mode install would look changed.
    """
    declared = installer._entity_files(entity)
    if not skipped:
        return declared
    omitted = {str(item) for item in skipped}
    return {path: digest for path, digest in declared.items() if path not in omitted}


def _modification_state(row: dict) -> dict:
    """Return the local modification state of one registry row.

    A failure degrades to the recorded flag: the check must keep running
    over the other installations even when one folder is unreadable.
    """
    try:
        return conflicts.detect_local_modifications(
            str(row.get("type") or "skill"),
            str(row.get("runtime_slug") or ""),
        )
    except Exception:
        return {
            "modified": bool(row.get("user_modified")),
            "files": list(row.get("user_modified_files_json") or []),
            "missing": [],
        }


def _metadata_newer(row: dict, entity: dict) -> bool:
    """True when the catalog manifest is newer than the installed record.

    This is the "version unchanged, metadata refreshed" case: the publisher
    edited the description, the licence text or a translation, and there is
    nothing to install. The platform does not compare descriptions itself -
    only the timestamps the two sides declare.
    """
    catalog_stamp = store.parse_isoformat(
        str(entity.get("updated_at") or entity.get("released_at") or "")
    )
    installed_stamp = store.parse_isoformat(str(row.get("updated_at") or ""))
    if catalog_stamp is None or installed_stamp is None:
        return False
    return catalog_stamp > installed_stamp


def _base_item(row: dict, entity: dict) -> dict:
    """Return the fields every report entry carries."""
    type_ = str(row.get("type") or "skill")
    return {
        "type": type_,
        "id": str(row.get("id") or ""),
        "agent_kind": (
            str(row.get("agent_kind") or "orchestrator") if type_ == "agent" else ""
        ),
        "name": installer._display_name(entity) or str(row.get("id") or ""),
        "runtime_slug": str(row.get("runtime_slug") or ""),
        "installed_version": str(row.get("version") or ""),
        "version": str(entity.get("version") or ""),
        "channel": str(row.get("channel") or ""),
    }


# ─── Checking ─────────────────────────────────────────────────────────────

def check_entity_updates(
    channel: Optional[str] = None,
    source: Optional[source_mod.MarketplaceSource] = None,
) -> dict:
    """Compare every installed entity with the catalog and report the gaps.

    Parameters:
        channel: release channel asked from the catalog; ``None`` uses the
            configured one.
        source: catalog transport; tests inject their own, production
            callers leave it ``None``.

    Returns a dict keyed by entity type (``skill``, ``agent``, ``rag``,
    ``mcp``) with one entry per available update, plus the bookkeeping
    keys: ``ok``, ``channel``, ``checked``, ``total``, ``items``,
    ``restamped``, ``unavailable`` and ``skipped`` (pinned, removed,
    unsupported).

    Side effects: a re-stamped version is written to the registry row
    (D-22). Nothing is downloaded and no file is touched.
    """
    settings = marketplace_config.marketplace_settings()
    if not settings.get("enabled"):
        raise UpdateError("marketplace_disabled", "The marketplace is disabled in the settings")

    chosen_channel = str(channel or settings.get("channel") or "main")
    transport = source or source_mod.default_source()
    report: Dict[str, object] = {type_: [] for type_ in registry.ENTITY_TYPES}
    report.update({
        "ok": True,
        "channel": chosen_channel,
        "checked": 0,
        "items": [],
        "total": 0,
        "restamped": [],
        "unavailable": [],
        "skipped": {"pinned": 0, "removed": 0, "unsupported": 0},
    })

    for row in registry.list_installed():
        type_ = str(row.get("type") or "")
        entity_id = str(row.get("id") or "")
        if not type_ or not entity_id:
            continue
        if row.get("pinned"):
            report["skipped"]["pinned"] += 1
            continue
        if type_ not in CHECKED_TYPES:
            report["skipped"]["unsupported"] += 1
            continue
        try:
            if registry.is_removed(type_, entity_id):
                report["skipped"]["removed"] += 1
                continue
        except Exception:
            pass

        report["checked"] += 1
        try:
            entity = transport.fetch_entity(type_, entity_id, None)
        except Exception as exc:
            report["unavailable"].append({
                "id": entity_id,
                "reason": str(getattr(exc, "code", "unavailable")),
            })
            continue

        item = _compare_row(row, entity, chosen_channel, report)
        if item is not None:
            report[type_].append(item)

    items: List[dict] = []
    for type_ in registry.ENTITY_TYPES:
        items.extend(report[type_])
    report["items"] = items
    report["total"] = len(items)
    return report


def _compare_row(row: dict, entity: dict, channel: str, report: Dict[str, object]) -> Optional[dict]:
    """Return the update entry of one row, or None when there is nothing.

    ``report`` is patched in place for the two outcomes that are not shown
    as updates: a re-stamped version (D-22) and an unparsable version pair.
    """
    entity_id = str(row.get("id") or "")
    installed_version = str(row.get("version") or "0.0.0")
    catalog_version = str(entity.get("version") or "0.0.0")
    try:
        comparison = semver.compare(installed_version, catalog_version)
    except semver.SemverError:
        report["unavailable"].append({"id": entity_id, "reason": "invalid_version"})
        return None

    if comparison > 0:
        # The installation is ahead of the catalog: a downgrade is never
        # offered as an update.
        return None

    if comparison == 0:
        if not _metadata_newer(row, entity):
            return None
        item = _base_item(row, entity)
        item["action"] = "metadata"
        item["message"] = "The catalog description was refreshed"
        return item

    compatible, reason = installer._platform_compatible(entity)
    if not compatible:
        item = _base_item(row, entity)
        item["action"] = "platform_required"
        item["required_platform"] = reason
        item["message"] = reason
        return item

    declared = _installed_subset(entity, row.get("skipped_code_files_json") or [])
    installed_tree = str(row.get("tree_sha256") or "")
    if declared and installed_tree and registry.manifest_sha256(declared) == installed_tree:
        _restamp(row, entity)
        report["restamped"].append({
            "id": entity_id,
            "from": installed_version,
            "to": catalog_version,
        })
        return None

    changes = _modification_state(row)
    licence = entity.get("license") if isinstance(entity.get("license"), dict) else {}
    item = _base_item(row, entity)
    item.update({
        "action": "update",
        "channel": channel,
        "user_modified": bool(changes["modified"]),
        "modified_files": list(changes["files"]),
        "requires_choice": bool(changes["modified"]),
        "license_required": bool(licence.get("acceptance_required"))
        and not row.get("license_accepted_at"),
        "size_bytes": int((entity.get("package") or {}).get("size") or 0),
    })
    return item


def _restamp(row: dict, entity: dict) -> None:
    """Record a new catalog version whose files are identical (D-22).

    The publisher bumped the version without touching a single byte - a
    rebuilt manifest, a tagging mistake, a re-published package. Installing
    it would download the same files over themselves, so the version is
    written to the row instead and the user is never shown a no-op update.
    """
    package = entity.get("package") if isinstance(entity.get("package"), dict) else {}
    fields = {
        "version": str(entity.get("version") or ""),
        "updated_at": store.isoformat_utc(),
    }
    package_sha = str(package.get("sha256") or "")
    if package_sha:
        fields["package_sha256"] = package_sha
    try:
        registry.update_installed(str(row.get("type") or "skill"), str(row.get("id") or ""), **fields)
    except Exception:
        # A failed re-stamp is not worth failing the whole check: the entry
        # simply shows up as an update on the next run.
        pass


# ─── Verifying ────────────────────────────────────────────────────────────

def verify_installed(
    type_: Optional[str] = None,
    *,
    record: bool = False,
) -> dict:
    """Compare every installation with the files it was installed from.

    Parameters:
        type_: limit the check to one entity type; ``None`` checks all.
        record: write the outcome back to the registry row. The default is
            read-only so a caller can render the state without a write;
            the updates page passes ``True`` to keep the card badge honest.

    Returns ``{"ok", "checked", "clean", "modified": [...], "entries":
    [...]}``, one entry per installation with the files that differ from
    the installed manifest and the files that disappeared.

    A strict-mode installation (``allow_code = false``) only ever compares
    the files it installed, so it is never reported as modified because of
    the code files that were deliberately left out.
    """
    rows = registry.list_installed(type_)
    entries: List[dict] = []
    for row in rows:
        state = _modification_state(row)
        entry = {
            "type": str(row.get("type") or "skill"),
            "id": str(row.get("id") or ""),
            "runtime_slug": str(row.get("runtime_slug") or ""),
            "modified": bool(state["modified"]),
            "files": list(state["files"]),
            "missing": list(state["missing"]),
        }
        entries.append(entry)
        if record:
            _record_state(row, entry)

    modified = [entry for entry in entries if entry["modified"]]
    return {
        "ok": True,
        "checked": len(entries),
        "clean": len(entries) - len(modified),
        "modified": modified,
        "entries": entries,
    }


def _record_state(row: dict, entry: dict) -> None:
    """Persist the detected modification state on the registry row."""
    current_files = sorted(str(item) for item in (row.get("user_modified_files_json") or []))
    if bool(row.get("user_modified")) == bool(entry["modified"]) and current_files == sorted(entry["files"]):
        return
    try:
        registry.update_installed(
            str(row.get("type") or "skill"),
            str(row.get("id") or ""),
            user_modified=bool(entry["modified"]),
            user_modified_files_json=list(entry["files"]),
        )
    except Exception:
        pass


# ─── Applying ─────────────────────────────────────────────────────────────

def apply_entity_updates(
    ids: Iterable[str],
    policy: str = "keep_mine",
    *,
    channel: Optional[str] = None,
    source: Optional[source_mod.MarketplaceSource] = None,
    accept_license: bool = False,
) -> dict:
    """Install the catalog version for every entity in *ids*.

    Parameters:
        ids: catalog ids to update. Ids that are not installed, or whose
            plan refuses the update, are reported instead of raising - one
            broken entity must not cancel the rest of the batch.
        policy: what to do when the user edited files - ``keep_mine``
            updates only the untouched files, ``take_catalog`` backs the
            edits up and replaces them, ``skip`` leaves the entity alone.
        channel: release channel; ``None`` uses the configured one.
        source: catalog transport; ``None`` uses the configured catalog.
        accept_license: explicit licence acceptance for entities that
            demand it. An entity accepted at install time stays accepted.

    Returns ``{"ok", "applied", "skipped", "failed", "results"}``. Every
    applied entry carries the run's ``install_id``, so the page can offer
    the undo straight away.
    """
    if policy not in POLICIES:
        raise UpdateError(
            "invalid_policy",
            f"Unknown update policy: {policy}",
            {"policies": list(POLICIES)},
        )
    settings = marketplace_config.marketplace_settings()
    if not settings.get("enabled"):
        raise UpdateError("marketplace_disabled", "The marketplace is disabled in the settings")

    chosen_channel = str(channel or settings.get("channel") or "main")
    transport = source or source_mod.default_source()
    results: List[dict] = []
    for entity_id in list(ids or []):
        results.append(
            _apply_one(str(entity_id), policy, chosen_channel, transport, accept_license)
        )

    applied = [item for item in results if item.get("ok") and not item.get("skipped")]
    skipped = [item for item in results if item.get("skipped")]
    failed = [item for item in results if not item.get("ok")]
    return {
        "ok": not failed,
        "applied": applied,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }


def _resolve_row(ref: str):
    """Return ``(type, id, row)`` for one update reference.

    A reference is either a bare catalog id or ``type:id``. The bare form is
    what the page sends and it keeps working as long as the id is installed
    once: the lookup scans the registry instead of assuming a type, which is
    what made every non-skill update answer ``not_installed``.
    """
    text = str(ref or "")
    prefix, _sep, rest = text.partition(":")
    if rest and prefix in registry.ENTITY_TYPES:
        return (prefix, rest, registry.get_installed(prefix, rest))
    for type_ in registry.ENTITY_TYPES:
        row = registry.get_installed(type_, text)
        if row is not None:
            return (type_, text, row)
    return (None, text, None)


def _apply_one(
    ref: str,
    policy: str,
    channel: str,
    transport: source_mod.MarketplaceSource,
    accept_license: bool,
) -> dict:
    """Update one entity and return its report entry."""
    type_, entity_id, row = _resolve_row(ref)
    if row is None or not type_:
        return {
            "ok": False,
            "id": entity_id,
            "error_code": "not_installed",
            "message": "The entity is not installed",
        }
    try:
        entity = transport.fetch_entity(type_, entity_id, None)
    except Exception as exc:
        return {
            "ok": False,
            "id": entity_id,
            "error_code": str(getattr(exc, "code", "entity_unavailable")),
            "message": str(getattr(exc, "message", exc)),
        }

    licence_ok = bool(accept_license or row.get("license_accepted_at"))
    plan = conflicts.resolve_plan(entity, type_=type_, accept_license=licence_ok)
    action = str(plan.get("action") or "")

    if action == "noop":
        return {"ok": True, "skipped": True, "id": entity_id, "reason": "noop"}
    if action == "modified_hold":
        return {"ok": True, "skipped": True, "id": entity_id, "reason": "modified_hold"}
    if action in ("downgrade_blocked", "platform_incompatible", "license_required",
                  "suffix_install", "adopt", "install"):
        # An update never installs something else: a plan that wants a fresh
        # install, a suffix or a forced downgrade is a decision for the
        # showcase card, not for the batch updater.
        return {
            "ok": False,
            "id": entity_id,
            "error_code": action,
            "message": str(plan.get("message") or action),
        }

    choice = "keep_mine"
    if action == "conflict_choice":
        if policy == "skip":
            return {"ok": True, "skipped": True, "id": entity_id, "reason": "user_modified"}
        if type_ != "skill" and policy != "take_catalog":
            # A base and an agent are replaced as a whole: their installers
            # have no per-file keep step, so pretending to honour the policy
            # would overwrite exactly the files the user edited.
            return {"ok": True, "skipped": True, "id": entity_id, "reason": "user_modified"}
        choice = "take_catalog" if policy == "take_catalog" else "keep_mine"

    try:
        if type_ == "skill":
            report = conflicts.resolve_three_way(
                choice,
                entity,
                channel=channel,
                source=transport,
                accept_license=licence_ok,
            )
        else:
            # A base or an agent owns files the three-path resolver does not
            # know: an index to rebuild, an orchestrator row to keep. Their
            # own installer already performs the staged replace, the rollback
            # and the registry write, so an update is a re-install of the
            # newer version over the installed folder.
            report = installer.install_entity(
                entity,
                channel=channel,
                source=transport,
                accept_license=licence_ok,
            )
    except Exception as exc:
        return {
            "ok": False,
            "id": entity_id,
            "error_code": str(getattr(exc, "code", "update_failed")),
            "message": str(getattr(exc, "message", exc)),
        }

    install_id = str(report.get("install_id") or "")
    if install_id:
        # The run was recorded by the conflict path; relabel it so the
        # journal says "update" rather than naming the chosen path.
        try:
            store.update_run(
                install_id,
                action="update",
                to_version=str(entity.get("version") or ""),
                policy=choice,
            )
        except Exception:
            pass
    return {
        "ok": True,
        "skipped": False,
        "id": entity_id,
        "name": installer._display_name(entity) or entity_id,
        "version": str(entity.get("version") or ""),
        "install_id": install_id,
        "policy": choice,
        "kept_files": list(report.get("kept_files") or []),
        "replaced_files": list(report.get("replaced_files") or []),
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
