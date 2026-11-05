"""
sagaai agents - which employees this installation can address.

The list is the FIRST thing a user needs before ``sagaai run``: the command
takes a slug, and a slug is not something a person can guess from the name
shown in the interface. The command exists for that single lookup and does
nothing else - it does not create, edit or delete an employee, because that
needs the folder sync and the marketplace rules which belong to the UI and to
DevAgent.

The listing goes through ``core.gateway.entities``, the same call the HTTP
API uses, so a slug accepted here is a slug the API accepts too.
"""

from __future__ import annotations

import argparse

from sagaai import exit_codes
from sagaai.commands import _common

SUMMARY = "list the employees a task can be given to"
DESCRIPTION = (
    "Prints the slug, the name, the built-in flag and the step limit of every "
    "installed employee. The slug is the value ``sagaai run --agent`` expects; "
    "the same list is what the HTTP API accepts under the token scope."
)


def register(parser: argparse.ArgumentParser, add_global_flags) -> None:
    """Declare the arguments of ``sagaai agents``."""
    parser.add_argument("--all", action="store_true", dest="show_all",
                        help="also print the description of every employee")
    add_global_flags(parser)


def execute(args: argparse.Namespace, console) -> int:
    """List the installed employees."""
    from core.gateway import entities

    cards = _common.call(entities.list_orchestrators)
    items = [card.to_dict() for card in cards]
    if not console.json_mode:
        console.title("employees")
        if not items:
            console.line("  no employees are installed yet")
            console.line("  start the platform once to seed the bundled ones: "
                         "sagaai ui")
        else:
            console.table(
                ["slug", "name", "builtin", "max_steps"],
                [[item.get("slug"), item.get("name"),
                  "yes" if item.get("is_builtin") else "no",
                  item.get("max_steps")] for item in items],
            )
            if getattr(args, "show_all", False):
                console.blank()
                for item in items:
                    console.title(str(item.get("slug")))
                    console.field("name", item.get("name"))
                    console.field("description", item.get("description"))
                    console.field("tools", ", ".join(item.get("tools") or []))
                    console.field("auto_apply", item.get("auto_apply"))
    console.emit("agents", {"agents": items, "count": len(items)},
                 exit_code=exit_codes.OK, version=_common.platform_version())
    return exit_codes.OK
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
