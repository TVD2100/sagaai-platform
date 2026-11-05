# -*- coding: utf-8 -*-
"""Repair the shape of a database created before the current table names.

This is the oldest revision of the chain and the only one that runs against a
schema nobody designed: a database written by an early build may call the
assistant table ``skills``, may call the thread columns ``skill_id`` and
``skill_name``, and may have a ``threads`` table without the ``type`` column
that can be added in place.

The three repairs are the ones the platform used to run on every start. They
live here so they run once per database, are recorded in the history, and
cannot be reordered by accident. On a database that already uses the current
names every step is a no-op.

Downgrade is not implemented on purpose: the inverse of a repair is "the
previous shape of an unknown foreign installation". A build that could produce
that shape no longer exists, so an attempt to recreate it would lose data for
no benefit. The revision is marked irreversible in :data:`IRREVERSIBLE`.
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

#: Machine-readable marker: this revision has no honest inverse.
IRREVERSIBLE = True


def upgrade() -> None:
    """Bring the legacy table and column names to the current ones."""
    from storage import db as storage_db

    bind = op.get_bind()
    # Order matters and matches the startup sequence this replaces: a table
    # without ``type`` is dropped first, so the rename pass below never sees
    # half-renamed columns of a table that is about to be rebuilt.
    storage_db._migrate_threads_table_if_needed(bind)
    storage_db._migrate_assistant_table(bind)
    storage_db._migrate_thread_skill_columns(bind)


def downgrade() -> None:
    """Refuse: the previous shape belongs to a build that no longer exists.

    Raises:
        NotImplementedError: always, with the reason.
    """
    raise NotImplementedError(
        "revision 0001 repairs legacy names and has no inverse; restore the "
        "snapshot taken before the migration instead")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
