"""Own asyncio scheduler: one tick per minute; all job state lives in the database."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import settings
from bot.scheduler import jobs
from bot.services.common import kv_get, kv_set, notify_admins_t
from bot.utils.timeutil import utcnow

log = logging.getLogger(__name__)

TICK_SECONDS = 60


def write_heartbeat(now: datetime) -> None:
    try:
        settings.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        settings.heartbeat_file.write_text(now.isoformat(), encoding="utf-8")
    except OSError as exc:
        log.warning("Cannot write heartbeat: %s", exc)


async def _safe(name: str, coro) -> int:
    try:
        return int(await coro or 0)
    except Exception:  # noqa: BLE001 - one failing job must not stop the others
        log.exception("Job %s failed", name)
        return 0


async def tick(bot: Bot, sm: async_sessionmaker[AsyncSession], now: datetime | None = None) -> dict[str, int]:
    now = now or utcnow()
    result = {
        "orders_expired": await _safe("expire_orders", jobs.job_expire_orders(bot, sm, now)),
        "subs_expired": await _safe("expirations", jobs.job_expirations(bot, sm, now)),
        "reminders": await _safe("sub_reminders", jobs.job_sub_reminders(bot, sm, now)),
        "access_retried": await _safe("access_retry", jobs.job_access_retry(bot, sm, now)),
        "review_reminders": await _safe("review_reminder", jobs.job_review_reminder(bot, sm, now)),
    }
    await _safe("backup", jobs.run_daily(bot, sm, now, "last_backup_date", settings.backup_hour, jobs.job_backup))
    await _safe("reconcile", jobs.run_daily(bot, sm, now, "last_reconcile_date", settings.reconcile_hour,
                                            jobs.job_reconcile))
    async with sm() as session:
        await kv_set(session, "heartbeat", now.isoformat())
        await session.commit()
    write_heartbeat(now)
    return result


async def catch_up(bot: Bot, sm: async_sessionmaker[AsyncSession]) -> dict[str, int | str]:
    """Run once at startup: process everything that happened while the bot was off."""
    now = utcnow()
    async with sm() as session:
        last = await kv_get(session, "heartbeat")
    downtime_min = int((now - datetime.fromisoformat(last)).total_seconds() // 60) if last else 0
    interrupted = await _safe("broadcasts", jobs.mark_interrupted_broadcasts(sm, now))
    joins = await _safe("recover_joins", jobs.recover_join_requests(bot, sm, now))
    counts = await tick(bot, sm, now)
    counts["joins_approved"] = joins
    counts["broadcasts_interrupted"] = interrupted
    counts["downtime_min"] = downtime_min
    return counts


async def announce_start(bot: Bot, sm: async_sessionmaker[AsyncSession], counts: dict[str, int | str]) -> None:
    missed = sum(int(v) for k, v in counts.items() if k != "downtime_min")
    async with sm() as session:
        await notify_admins_t(
            bot, session, "admin_bot_started", missed=missed,
            downtime=counts.get("downtime_min", 0),
            orders=counts.get("orders_expired", 0), subs=counts.get("subs_expired", 0),
            reminders=counts.get("reminders", 0), joins=counts.get("joins_approved", 0),
            access=counts.get("access_retried", 0), broadcasts=counts.get("broadcasts_interrupted", 0),
        )


class Scheduler:
    def __init__(self, bot: Bot, sm: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.sm = sm
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await tick(self.bot, self.sm)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Scheduler tick failed")

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
