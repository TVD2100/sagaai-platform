"""Pure helpers behind the connection-tool registry and memory writes.

Both areas boil down to small decisions: which connection services are
enabled, which tool names belong to a service family, whether an owner-profile
rewrite is allowed and how an append concatenates two texts. Keeping those
decisions here leaves the tool methods as a straight line of calls, so each
rule is readable on its own and testable without a dispatcher.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Sequence, Set

_TRUTHY = ("1", "true", "yes", "on")


def normalize_overwrite(value: Any) -> bool:
    """Return the boolean form of ``overwrite_owner_profile``.

    The model may send the flag as a string (``"true"``/``"1"``), so a plain
    bool() would read ``"false"`` as True. Only an explicit truthy spelling
    counts as consent.
    """
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return bool(value)


def merge_append(existing: str, text: str) -> str:
    """Return *text* appended to *existing* with one blank line between.

    Both parts are stripped of trailing newlines first, so repeated appends
    never accumulate blank lines, and the result always ends with one newline
    when it is not empty.
    """
    head = (existing or "").rstrip()
    block = (text or "").strip("\n")
    if not block:
        return head + "\n" if head else ""
    return f"{head}\n\n{block}\n" if head else f"{block}\n"


def enabled_services(enabled_ids: Iterable[Any],
                     get_connection: Callable[[Any], Any]) -> Set[str]:
    """Return the service names of the enabled connections.

    A connection that cannot be read is skipped: one broken record must not
    hide the tools of the healthy ones.
    """
    services: Set[str] = set()
    for conn_id in enabled_ids or ():
        try:
            conn = get_connection(conn_id)
        except Exception:
            continue
        if isinstance(conn, dict) and conn.get("service"):
            services.add(str(conn["service"]))
    return services


def drop_family(extra: Dict[str, Any], names: Sequence[str]) -> None:
    """Remove every tool of one family from the registry."""
    for name in names:
        extra.pop(name, None)


def register_family(extra: Dict[str, Any], names: Sequence[str],
                    module: Any) -> int:
    """Register the callable tools of *names* found in *module*.

    Returns the number of registered tools; a missing or non-callable entry is
    skipped instead of failing the whole attachment.
    """
    registered = 0
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            extra[name] = fn
            registered += 1
    return registered


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
