# -*- coding: utf-8 -*-
"""Create the marketplace registry tables on the main database.

The registry is read and written through raw SQL, so its tables never came
from the ORM models and were created by a side effect of starting the
platform. Here they become a named step of the history.

``runtime_db_id`` is TEXT rather than INTEGER because the primary keys of
``assistants`` and ``orchestrators`` are ``String(8)``: an integer column
could never hold them.

The revision runs on ``sagaai.db`` only. ``devagent.db`` shares the ORM
tables, but nothing in the agent database reads the registry - the platform
keeps installed packages in the main database alone.
"""
from __future__ import annotations

from alembic import op

from storage.migration_support import is_main_database

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the registry tables and the ``agent_kind`` column."""
    if not is_main_database():
        return
    from storage import db as storage_db

    storage_db._ensure_marketplace_tables(op.get_bind())


def downgrade() -> None:
    """Drop the registry tables (the registry is a cache of installed files)."""
    if not is_main_database():
        return
    op.drop_table("marketplace_removed")
    op.drop_table("marketplace_installed")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
