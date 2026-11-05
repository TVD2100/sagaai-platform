# -*- coding: utf-8 -*-
"""Create the scheduled-run tables on the main database.

The scheduler stores its rules outside the ORM on purpose: claiming a due
point needs one atomic ``UPDATE ... WHERE`` inside a ``BEGIN IMMEDIATE``
transaction, and ``UNIQUE(schedule_id, planned_at)`` is what makes one
scheduled point produce at most one history row however often a tick repeats
or however the process dies between claiming and starting.

Like the marketplace registry, the tables belong to ``sagaai.db`` alone: the
scheduler is a thread of the interface, and the agent database holds no rule.
"""
from __future__ import annotations

from alembic import op

from storage.migration_support import is_main_database

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the schedule tables and their indexes."""
    if not is_main_database():
        return
    from storage import db as storage_db

    storage_db._ensure_schedules_tables(op.get_bind())


def downgrade() -> None:
    """Drop the schedule tables (their rows are the run history)."""
    if not is_main_database():
        return
    op.drop_table("schedule_runs")
    op.drop_table("schedules")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
