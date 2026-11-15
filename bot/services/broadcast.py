"""Broadcasts with rate limiting and a delivery report."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import settings
from bot.db.models import Broadcast, Subscription, User
from bot.i18n import t
from bot.services.common import audit, mark_blocked
from bot.services.subscriptions import active_user_ids
from bot.utils.timeutil import utcnow

log = logging.getLogger(__name__)

SEGMENTS = ("all", "active", "expired")
_tasks: set[asyncio.Task] = set()


async def recipients(session: AsyncSession, segment: str, now: datetime) -> list[int]:
    users = set((await session.scalars(select(User.id).where(User.is_blocked.is_(False)))).all())
    if segment == "all":
        return sorted(users)
    active = await active_user_ids(session, now)
    if segment == "active":
        return sorted(users & active)
    ever = set((await session.scalars(select(Subscription.user_id).distinct())).all())
    return sorted(users & (ever - active))


async def run_broadcast(bot: Bot, sm: async_sessionmaker[AsyncSession], broadcast_id: int, admin_lang: str) -> None:
    async with sm() as session:
        bc = await session.get(Broadcast, broadcast_id)
        ids = await recipients(session, bc.segment, utcnow())
        bc.total = len(ids)
        await session.commit()
        delay = 1.0 / settings.broadcast_rate_per_second
        sent = failed = 0
        for n, uid in enumerate(ids, start=1):
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=bc.from_chat_id, message_id=bc.message_id)
                sent += 1
            except TelegramForbiddenError:
                failed += 1
                await mark_blocked(session, uid)
            except (TelegramBadRequest, TelegramAPIError) as exc:
                failed += 1
                log.info("Broadcast to %s failed: %s", uid, exc)
            if n % 25 == 0:
                bc.sent, bc.failed = sent, failed
                await session.commit()
            await asyncio.sleep(delay)
        bc.sent, bc.failed = sent, failed
        bc.status = "done"
        bc.finished_at = utcnow()
        await audit(session, bc.admin_id, "broadcast", f"broadcast:{bc.id}",
                    f"segment={bc.segment} sent={sent} failed={failed}")
        await session.commit()
        try:
            await bot.send_message(bc.admin_id, t(admin_lang, "bc_report", total=bc.total, sent=sent, failed=failed))
        except TelegramAPIError:
            pass


def start_broadcast(bot: Bot, sm: async_sessionmaker[AsyncSession], broadcast_id: int, admin_lang: str) -> None:
    task = asyncio.create_task(run_broadcast(bot, sm, broadcast_id, admin_lang), name=f"broadcast-{broadcast_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
