# -*- coding: utf-8 -*-
"""Create the sandbox run journal on the main database.

The journal is read and written through raw SQL, like the marketplace and
schedule tables, because a run is recorded from a child-process boundary
where an ORM session would outlive the unit of work it describes.

A refused run is a row as well: the table exists to make the sandbox's
decisions auditable, and a denial that leaves no trace would make the
fail-closed guarantee unverifiable after the fact.

The table belongs to ``sagaai.db`` alone - the agent database keeps dialogs,
not execution history.
"""
from __future__ import annotations

from alembic import op

from storage.migration_support import is_main_database

revision = "0009"
# A revision must hang off the newest one in the chain, otherwise Alembic
# reports two heads and a fresh database can no longer be created.
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the journal table and its indexes."""
    if not is_main_database():
        return
    from storage import db as storage_db

    storage_db._ensure_sandbox_tables(op.get_bind())


def downgrade() -> None:
    """Drop the journal (its rows are the run history)."""
    if not is_main_database():
        return
    op.drop_table("sandbox_runs")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
