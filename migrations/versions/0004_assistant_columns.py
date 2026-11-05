# -*- coding: utf-8 -*-
"""Add the ``assistants`` columns introduced after the assistant folders.

``slug`` names the folder an assistant lives in and ``reasoning_effort``
records the model's thinking budget. Both are nullable, so a row written
before them reads as NULL - which is the honest answer, not a default value
invented at migration time.
"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the missing ``assistants`` columns."""
    from storage import db as storage_db

    storage_db._ensure_assistant_columns(op.get_bind())


def downgrade() -> None:
    """Refuse: the columns identify an assistant's folder and its budget.

    Raises:
        NotImplementedError: always.
    """
    raise NotImplementedError(
        "revision 0004 adds the assistant folder and reasoning columns; "
        "restore the snapshot instead")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
