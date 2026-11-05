# -*- coding: utf-8 -*-
"""Helpers shared by the revisions of the schema history.

A revision body runs with an Alembic ``op`` proxy, not with the installation
context: it has a connection and nothing else. Two facts a revision needs are
therefore read from the Alembic config here instead of guessed:

* the kind of database it upgrades - the marketplace and schedule tables
  belong to the main database only, and creating them in ``devagent.db``
  would leave two tables nothing ever reads;
* whether a table exists at all, which is what makes a revision safe to run
  on a database that already lived through the change.

Keeping the lookups in one module means a revision reads as history, not as
boilerplate, and the module is importable by its package path (the revision
scripts themselves are loaded by path, so a sibling import would not work).
"""
from __future__ import annotations

from alembic import context, op
from sqlalchemy import inspect

#: Kinds a revision distinguishes.
MAIN_KIND = "main"
DEVAGENT_KIND = "devagent"


def database_kind() -> str:
    """Return the kind of database the current migration targets.

    ``storage.migrations`` puts the kind into ``config.attributes`` before
    ``upgrade`` runs; a command-line run passes ``-x db_kind=...``. An absent
    value means the main database, which is the only database the command
    line can be pointed at without saying so.
    """
    attributes = context.config.attributes
    if attributes.get("db_kind"):
        return str(attributes["db_kind"])
    try:
        arguments = dict(context.get_x_argument(as_dictionary=True) or {})
    except Exception:  # pragma: no cover - not every context supports it
        arguments = {}
    return str(arguments.get("db_kind") or MAIN_KIND)


def is_main_database() -> bool:
    """True when the revision runs against ``sagaai.db``."""
    return database_kind() == MAIN_KIND


def table_exists(name: str) -> bool:
    """True when the database the revision runs on already has *name*."""
    return str(name) in set(inspect(op.get_bind()).get_table_names())
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
