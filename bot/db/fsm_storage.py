"""aiogram FSM storage kept in SQLite, so half-finished dialogs survive restarts."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import FSMRecord


def _key(key: StorageKey) -> str:
    return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id or 0}:{key.destiny}"


class SQLiteStorage(BaseStorage):
    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = session_maker

    async def _get(self, session: AsyncSession, key: StorageKey) -> FSMRecord:
        record = await session.get(FSMRecord, _key(key))
        if record is None:
            record = FSMRecord(key=_key(key), state=None, data="{}")
            session.add(record)
        return record

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        async with self._sm() as session:
            record = await self._get(session, key)
            record.state = value
            await session.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._sm() as session:
            record = await session.get(FSMRecord, _key(key))
            return record.state if record else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        async with self._sm() as session:
            record = await self._get(session, key)
            record.data = json.dumps(dict(data), ensure_ascii=False, default=str)
            await session.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._sm() as session:
            record = await session.get(FSMRecord, _key(key))
            return json.loads(record.data) if record and record.data else {}

    async def close(self) -> None:
        return None
