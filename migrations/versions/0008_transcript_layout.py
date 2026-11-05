# -*- coding: utf-8 -*-
"""Record that the message layout moved from tables to transcript files.

The move itself is not a schema change: ``core.chat_db_archive`` and
``core.devagent_db_archive`` write every dialog into a per-thread transcript,
fill the index columns of ``threads`` and only then drop the rows they
copied. It also replaces the ``devagent.db`` file when that file is still in
the old layout, which cannot happen inside the transaction of a revision -
so the driver runs the archivers BEFORE it starts the chain.

The revision exists so the history says the layout was applied, and so a
database that never ran an archiver is visible as such. It changes no row:
by the time it runs, the archivers have either done their work or had
nothing to do, and both leave the same stamp (``PRAGMA user_version``).

Downgrade is empty for the same reason: the transcript files are the
dialogue, and a schema revision is not the place to move them back into
rows that may no longer exist.
"""
from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

#: The layout version the archivers stamp into ``PRAGMA user_version``.
#: Recorded here so a reader of the history can tell which layout this
#: revision introduced without opening an archiver module.
TRANSCRIPT_LAYOUT_VERSION = 1


def upgrade() -> None:
    """Nothing to change: the driver applied the archivers before the chain."""
    _ = op.get_bind()


def downgrade() -> None:
    """Nothing to undo: the layout lives in the archivers, not here."""
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
