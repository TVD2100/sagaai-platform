# -*- coding: utf-8 -*-
"""Create the per-thread message index on databases that predate it.

Loading one dialog reads ``messages`` filtered by ``thread_id`` ordered by
``id``. Without ``ix_messages_thread_id_id`` that is a full table scan, and
the scan grows with the installation's whole history rather than with the
dialog being opened. A fresh database gets the index from the ORM model;
this revision adds it to a database created before the index existed.
"""
from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the message index when it is absent."""
    from storage import db as storage_db

    storage_db._ensure_message_index(op.get_bind())


def downgrade() -> None:
    """Drop the index (safe: it changes speed, not content)."""
    op.drop_index("ix_messages_thread_id_id", table_name="messages")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
