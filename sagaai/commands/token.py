"""
sagaai token - issue, list and revoke the credentials of the HTTP API.

A token is the only credential the API accepts, and the store keeps only its
SHA-256: the plaintext exists exactly once, in the output of ``create``. The
command therefore prints it LAST, after every line that explains where it is
useful, because a person who scrolls past it cannot get it back - only the
revoke/create round trip remains.

The token created here is the same record the ``/v1`` routes verify: the
command calls ``core.api_token_store.create_token``, and the HTTP layer reads
the same table through the same module. No second store, no second format.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List

from sagaai import exit_codes
from sagaai.commands import _common
from sagaai.errors import CliError, config_error

SUMMARY = "issue, list and revoke API tokens"
DESCRIPTION = (
    "The HTTP API authenticates with a bearer token. ``create`` issues one and "
    "prints it ONCE - only its hash is stored, so the value cannot be shown "
    "again. ``list`` shows the records without their hashes, ``revoke`` "
    "removes one by its public id. A token with scope ``*`` reaches every "
    "employee; naming slugs narrows it to those employees."
)


def register(parser: argparse.ArgumentParser, add_global_flags) -> None:
    """Declare the actions of ``sagaai token``."""
    add_global_flags(parser)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    create = actions.add_parser(
        "create",
        help="issue a token and print it once",
        description=("Issue a token. The plaintext is printed once and never "
                     "shown again; copy it immediately."),
        epilog=exit_codes.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    create.add_argument("--scope", action="append", default=None,
                        metavar="SLUG",
                        help="employee slug this token may address (repeatable; "
                             "default: every employee)")
    create.add_argument("--note", default="",
                        help="a label for the owner, shown by ``token list``")
    create.add_argument("--expires", default="", metavar="YYYY-MM-DD",
                        help="expiry date; the token is valid through that day")
    add_global_flags(create)

    listing = actions.add_parser(
        "list",
        help="list the stored tokens",
        description="List the stored token records. Hashes are never shown.",
        epilog=exit_codes.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_global_flags(listing)

    revoke = actions.add_parser(
        "revoke",
        help="remove a token by its id",
        description="Remove one token record by the public id ``token list`` "
                    "shows.",
        epilog=exit_codes.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    revoke.add_argument("token_id", metavar="ID", help="public id of the token")
    add_global_flags(revoke)
    parser.set_defaults(action="")


def _create(args: argparse.Namespace, console) -> int:
    """Issue one token and print it exactly once."""
    from core import api_token_store as store

    scopes: List[str] = [str(item) for item in (getattr(args, "scope", None) or [])]
    hints: List[str] = []
    if scopes:
        installed = _common.employee_names()
        for slug in scopes:
            if installed and slug != "*" and slug not in installed:
                hints.append("unknown employee slug: " + slug)
    result = _common.call(store.create_token, slugs=scopes or None,
                          note=str(getattr(args, "note", "") or ""),
                          expires_at=str(getattr(args, "expires", "") or ""))
    if not result.get("ok"):
        raise config_error(str(result.get("code") or "token_failed"),
                           str(result.get("error") or "the token was not created"),
                           hint="sagaai token list")
    token = str(result.get("token") or "")
    record = dict(result.get("record") or {})
    data: Dict[str, Any] = {"token": token, "record": record,
                            "warnings": hints}
    if not console.json_mode:
        console.title("API token")
        console.field("id", record.get("id"))
        console.field("scope", ",".join(record.get("slugs") or []) or "-")
        console.field("expires", record.get("expires_at") or "never")
        console.field("note", record.get("note") or "-")
        for hint in hints:
            console.warn(hint)
        console.blank()
        console.title("the token (shown once)")
        console.line("  " + token)
        console.line("")
        console.line("  It cannot be shown again: only its hash is stored.")
        console.line("  Try it with:  curl -H \"Authorization: Bearer <token>\" "
                     "http://127.0.0.1:8765/v1/agents")
    console.emit("token create", data, exit_code=exit_codes.OK,
                 version=_common.platform_version())
    return exit_codes.OK


def _list(console) -> int:
    """List the stored token records."""
    from core import api_token_store as store

    tokens = _common.call(store.list_tokens)
    if not console.json_mode:
        console.title("API tokens")
        if not tokens:
            console.line("  no tokens are stored")
            console.line("  create one: sagaai token create --scope dev_agent")
        else:
            console.table(
                ["id", "scope", "created", "expires", "state", "note"],
                [[item.get("id"), ",".join(item.get("slugs") or []),
                  str(item.get("created_at") or "")[:19],
                  item.get("expires_at") or "never",
                  "expired" if item.get("expired") else "usable",
                  item.get("note") or ""] for item in tokens],
            )
    console.emit("token list", {"tokens": tokens, "count": len(tokens)},
                 exit_code=exit_codes.OK, version=_common.platform_version())
    return exit_codes.OK


def _revoke(args: argparse.Namespace, console) -> int:
    """Remove one token record by its public id."""
    from core import api_token_store as store

    token_id = str(getattr(args, "token_id", "") or "").strip()
    if not token_id:
        raise config_error("invalid_request", "a token id is required",
                           hint="sagaai token list")
    known = {str(item.get("id") or "") for item in _common.call(store.list_tokens)}
    if token_id not in known:
        raise config_error("unknown_token", "no token with id " + token_id,
                           hint="sagaai token list")
    if not _common.call(store.revoke, token_id):
        raise CliError("revoke_failed", "the token record could not be removed",
                       exit_code=exit_codes.FAILURE,
                       hint="check the data folder: sagaai doctor")
    data = {"id": token_id, "revoked": True}
    if not console.json_mode:
        console.title("API token")
        console.field("revoked", token_id)
        console.line("  A client using it now receives 401 invalid_token.")
    console.emit("token revoke", data, exit_code=exit_codes.OK,
                 version=_common.platform_version())
    return exit_codes.OK


def execute(args: argparse.Namespace, console) -> int:
    """Dispatch to one of the three token actions."""
    action = str(getattr(args, "action", "") or "")
    if action == "create":
        return _create(args, console)
    if action == "list":
        return _list(console)
    if action == "revoke":
        return _revoke(args, console)
    raise config_error("missing_action",
                       "choose an action: create, list or revoke",
                       hint="sagaai token --help")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
