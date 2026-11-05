# -*- coding: utf-8 -*-
"""Add the transcript-index columns and their indexes to ``threads``.

The columns describe where a dialog's messages are stored (a per-thread
transcript file) and what it costs. Every one of them is nullable and NULL
carries a meaning: it marks a legacy row whose messages are still in the
``messages`` table. A database that lived through the change already has
them; one from before receives them here, in place, without a rebuild and
without touching a single row.

Downgrade removes the columns. It is irreversible in practice for the same
reason the columns are meaningful: dropping them loses the transcript index
of every dialog, so the data is gone even if the files remain. Restore the
snapshot instead.
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the missing ``threads`` columns and the two listing indexes."""
    from storage import db as storage_db

    storage_db._ensure_thread_columns(op.get_bind())


def downgrade() -> None:
    """Refuse: dropping the index loses where every dialog's messages live.

    Raises:
        NotImplementedError: always.
    """
    raise NotImplementedError(
        "revision 0003 adds the transcript index; dropping it loses the "
        "location of every dialog's messages - restore the snapshot instead")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
