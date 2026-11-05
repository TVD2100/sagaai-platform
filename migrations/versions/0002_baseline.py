# -*- coding: utf-8 -*-
"""Stamp the schema of the platform: the baseline of the history.

The revision has two behaviours, and which one runs is decided by the file it
meets - not by a flag:

* an empty database (a fresh installation) is created here from the ORM
  metadata. This is the only place ``create_all`` is called: the schema has
  exactly one source, and it is the first revision of the history rather than
  a side effect of importing the storage module;
* a database that already has tables is left untouched. Its structure is
  what the following revisions inspect and repair, so stamping it here is
  safe and is what makes an un-versioned installation adopt the history.

Why the two indexes are created explicitly. ``ix_threads_parent_thread_id``
and ``ix_threads_kind`` are not declared on the ORM model - they exist to
speed up listing the children of a dialog and filtering by thread kind, and
were introduced by a startup statement. On a fresh database nothing has
created them yet, so the baseline does it and the result matches a database
that lived through every previous start.

Downgrade drops every ORM table. That is what "down to the base" means, and
it is destructive by definition - ``sagaai migrate`` takes a snapshot before
it touches a database, and the command line documents the snapshot restore.
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the schema on an empty database; stamp an existing one."""
    from sqlalchemy import inspect

    from storage import db as storage_db

    bind = op.get_bind()
    # ``alembic_version`` already exists by the time this revision runs - it
    # is the bookkeeping table, not a sign of a populated installation. The
    # presence of ``threads`` is what distinguishes the two cases: it is the
    # first table the platform ever created and every later table references
    # it or lives beside it.
    if "threads" in set(inspect(bind).get_table_names()):
        return  # an existing installation: its schema is the baseline
    storage_db.Base.metadata.create_all(bind)
    storage_db._ensure_thread_indexes(bind)


def downgrade() -> None:
    """Drop every ORM table (destructive: restore the snapshot afterwards)."""
    from storage import db as storage_db

    storage_db.Base.metadata.drop_all(op.get_bind())
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
