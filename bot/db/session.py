"""Async SQLAlchemy engine + session factory for SQLite (WAL mode)."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def _set_sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - trivial
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def make_engine(db_file: Path | str) -> AsyncEngine:
    if str(db_file) != ":memory:":
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(url, future=True)
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    return engine


def make_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
