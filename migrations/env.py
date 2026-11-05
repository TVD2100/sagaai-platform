# -*- coding: utf-8 -*-
"""Alembic environment of the SagaAI schema history.

The revision chain is executed against BOTH installation databases
(``sagaai.db`` and ``devagent.db``). They hold the same ORM tables; only the
raw-SQL feature tables (marketplace, schedules) belong to the main database
alone, and a revision tells them apart by the ``db_kind`` value.

Where the paths come from. A programmatic caller (``storage.migrations``)
sets ``config.attributes``; the ``alembic`` command line passes
``-x db_path=... -x db_kind=...`` instead. Neither the database file nor the
kind is written into ``alembic.ini``: the same file describes every
installation. The resolved values are written back into ``config.attributes``
so a revision reads the kind from one place.

Why ``foreign_keys`` stays off on this connection. ``render_as_batch``
rewrites an SQLite table as create-new / copy-rows / drop-old. With foreign
keys enforced, dropping a table another one references aborts the rebuild,
and a legacy database is exactly the case where a table is rebuilt. The
platform connection (``storage.db``) enforces the pragma on its own engine;
the migration connection deliberately does not.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, event, pool

from storage.models import Base

config = context.config

if config.config_file_name is not None:
    # ``disable_existing_loggers=False`` keeps the loggers the application
    # configured before the migration; the default would silence them.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

#: The ORM metadata is the target of ``compare_type`` and of the baseline
#: revision: a fresh database is created from the models, not from a copy of
#: the DDL that would drift from them.
target_metadata = Base.metadata

#: Recognised database kinds. ``main`` is ``sagaai.db``, ``devagent`` is
#: ``devagent.db``; an unknown kind is refused instead of silently treated as
#: the main database.
DB_KINDS = ("main", "devagent")

#: How long a migration waits for another process's lock before it fails.
#: Matches the application connection so a blocking instance behaves the same
#: way whichever side hits the lock.
BUSY_TIMEOUT_MS = 5000


def _x_arguments() -> dict:
    """Return the ``-x`` arguments of a command-line run (empty when none)."""
    try:
        return dict(context.get_x_argument(as_dictionary=True) or {})
    except Exception:  # pragma: no cover - not every context supports it
        return {}


def database_settings() -> tuple:
    """Return ``(url, kind)`` for the database this migration targets.

    ``config.attributes`` wins over the command-line ``-x`` values, so a
    programmatic caller cannot be shadowed by a stale command line. A missing
    path is a programming error: guessing the default installation database
    would let a stray ``alembic upgrade`` rewrite a live installation.
    """
    arguments = _x_arguments()
    attributes = config.attributes
    db_path = str(attributes.get("db_path") or arguments.get("db_path") or "").strip()
    kind = str(attributes.get("db_kind") or arguments.get("db_kind") or "main").strip()
    if kind not in DB_KINDS:
        raise ValueError("unknown database kind: %r (expected one of %s)"
                         % (kind, ", ".join(DB_KINDS)))
    if not db_path:
        raise ValueError(
            "no database path: pass config.attributes['db_path'] or -x db_path=...")
    # One resolution serves both the environment and the revision body.
    attributes["db_path"] = db_path
    attributes["db_kind"] = kind
    return "sqlite:///" + db_path, kind


def _options() -> dict:
    """Return the ``context.configure`` options shared by both modes."""
    return {
        "target_metadata": target_metadata,
        # SQLite cannot ALTER most things in place; batch mode rebuilds the
        # table instead, which is the only way a column rename survives.
        "render_as_batch": True,
        # A revision that fails leaves the previous one committed, so the
        # database is at an honest, known version instead of a half state.
        "transaction_per_migration": True,
        "compare_type": True,
        "include_schemas": False,
    }


def _prepare_connection(dbapi_connection, connection_record) -> None:
    """Set the per-connection pragmas before any transaction begins.

    ``PRAGMA foreign_keys`` is silently ignored inside a transaction, and
    SQLAlchemy opens one on the first statement. The ``connect`` event runs
    on the raw DBAPI connection, which is the only point where the pragma
    still has an effect.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)
        cursor.execute("PRAGMA foreign_keys=OFF")
    finally:
        cursor.close()


def run_migrations_offline() -> None:
    """Emit the migrations as SQL against a URL (``alembic upgrade --sql``)."""
    url, _kind = database_settings()
    context.configure(url=url, literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, **_options())
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run the migrations against the database file in place."""
    url, _kind = database_settings()
    engine = create_engine(url, poolclass=pool.NullPool,
                           connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _prepare_connection)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, **_options())
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
