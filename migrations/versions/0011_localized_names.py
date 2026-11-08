# -*- coding: utf-8 -*-
"""Add the localized name slots to ``orchestrators`` and ``assistants``.

Every entity carries a user-defined name (the existing ``name`` column) and
three translations - English, Russian, Chinese. The user-defined name is
what a locally created entity has; the translations are what a marketplace
entity ships. Resolution between them lives in ``core.naming``.

All three columns are nullable and a NULL means "no translation of this
kind", which is the honest state of every row written before localization.
No default is invented at migration time: an entity without translations
falls back to its user-defined name, exactly as it did before the change.

The tables live in the main database only, so the DevAgent database is
skipped.
"""
from __future__ import annotations

from alembic import op

from storage.migration_support import is_main_database

revision = "0011"
# Chains after the last schema revision of the current history. 0010 is the
# head this revision was written against.
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the missing localized-name columns to both entity tables."""
    if not is_main_database():
        return
    from storage import db as storage_db

    connection = op.get_bind()
    storage_db._ensure_orchestrator_columns(connection)
    storage_db._ensure_assistant_columns(connection)


def downgrade() -> None:
    """Leave the columns in place.

    This is deliberately a no-op, the same choice revision 0010 makes for
    its flag. SQLite before 3.35 cannot drop a column at all, and on a newer
    engine a drop would discard every translation a user entered after the
    upgrade. The revision marker moves back, so alembic reports the database
    as one revision behind and the next upgrade re-enters this module; the
    column helpers are idempotent, so re-upgrading is harmless.
    """
    return
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
