"""Tiny versioned migration system (simpler than Alembic for a single SQLite file).

To change the schema later: append a new function to MIGRATIONS. Each one runs
exactly once, in order, inside a transaction; the applied version is stored in
the ``schema_version`` table. Never edit or reorder migrations that already ran.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.db.models import Base

log = logging.getLogger(__name__)


def _m001_initial(conn: Connection) -> None:
    Base.metadata.create_all(conn)


MIGRATIONS: list[Callable[[Connection], None]] = [
    _m001_initial,
    # _m002_example: lambda conn: conn.execute(text("ALTER TABLE users ADD COLUMN note TEXT")),
]


def _apply(conn: Connection) -> int:
    conn.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"))
    row = conn.execute(text("SELECT version FROM schema_version")).first()
    current = row[0] if row else 0
    if row is None:
        conn.execute(text("INSERT INTO schema_version (version) VALUES (0)"))
    applied = 0
    for number, migration in enumerate(MIGRATIONS, start=1):
        if number <= current:
            continue
        log.info("Applying DB migration %s (%s)", number, migration.__name__)
        migration(conn)
        conn.execute(text("UPDATE schema_version SET version = :v"), {"v": number})
        applied += 1
    return applied


async def run_migrations(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        return await conn.run_sync(_apply)
