# -*- coding: utf-8 -*-
"""Record a run that was stopped from outside while it was executing.

A cancellation is a decision, not a limit of the profile, and the journal
has to tell it apart from a timeout: "the child ran too long" and "the user
stopped it" lead to different follow-up actions.

A fresh database already gets the column from the table definition, so the
upgrade adds it only when an older journal exists.
"""
from __future__ import annotations

from alembic import op

from storage.migration_support import is_main_database

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the ``cancelled`` flag to the sandbox journal."""
    if not is_main_database():
        return
    from storage import db as storage_db

    connection = op.get_bind()
    if not storage_db._sandbox_table_exists(connection):
        return
    columns = storage_db._sandbox_columns(connection)
    if "cancelled" not in columns:
        op.execute("ALTER TABLE sandbox_runs ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    """Drop the flag; SQLite cannot drop a column before 3.35."""
    if not is_main_database():
        return
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
