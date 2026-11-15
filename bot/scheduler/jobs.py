"""Scheduled jobs. Every job is time-based and idempotent, so running it late
(after the laptop slept or was off) processes exactly what is still relevant."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import settings
from bot.db.models import (
    Broadcast,
    Channel,
    ChannelMember,
    InviteLink,
    Order,
    OrderStatus,
    Plan,
    SubStatus,
    Subscription,
    User,
    plan_channels,
)
from bot.i18n import t
from bot.keyboards import AdminCB, renew_kb
from bot.services import access as access_svc
from bot.services import orders as order_svc
from bot.services.backup import make_backup
from bot.services.common import admin_languages, is_protected, kv_get, kv_set, notify_admins_t, safe_send
from bot.services.subscriptions import active_channel_ids, grace
from bot.utils.text import esc
from bot.utils.timeutil import fmt_dt, to_local

log = logging.getLogger(__name__)

SM = async_sessionmaker[AsyncSession]
ORDER_EXPIRED_NOTIFY_WINDOW = timedelta(hours=6)


# ------------------------------------------------------------------ orders
async def job_expire_orders(bot: Bot, sm: SM, now: datetime) -> int:
    async with sm() as session:
        expired = await order_svc.expire_orders(session, now)
        for order in expired:
            # Do not spam about very old orders after a long downtime.
            if now - order.expires_at <= ORDER_EXPIRED_NOTIFY_WINDOW:
                user = await session.get(User, order.user_id)
                if user and not user.is_blocked:
                    await safe_send(bot, session, user.id, t(user.language, "order_expired_user", code=order.code))
        await session.commit()
        return len(expired)


async def job_review_reminder(bot: Bot, sm: SM, now: datetime) -> int:
    """Remind admins about receipts waiting too long (one aggregated message, rate-limited)."""
    async with sm() as session:
        threshold = now - timedelta(hours=settings.review_remind_after_hours)
        waiting = (await session.scalars(
            select(Order).where(Order.status == OrderStatus.PENDING_REVIEW, Order.proof_submitted_at <= threshold)
            .order_by(Order.proof_submitted_at)
        )).all()
        if not waiting:
            return 0
        last = await kv_get(session, "review_reminder_at")
        if last and datetime.fromisoformat(last) > now - timedelta(hours=settings.review_remind_interval_hours):
            return 0
        codes = ", ".join(f"#{o.code}" for o in waiting[:15]) + ("…" if len(waiting) > 15 else "")

        def kb(lang: str):
            b = InlineKeyboardBuilder()
            b.button(text=t(lang, "btn_open_queue"), callback_data=AdminCB(s="orders", a="queue"))
            return b.as_markup()

        await notify_admins_t(bot, session, "admin_review_reminder", markup=kb, count=len(waiting),
                              hours=settings.review_remind_after_hours, codes=codes)
        await kv_set(session, "review_reminder_at", now.isoformat())
        await session.commit()
        return 1


async def job_access_retry(bot: Bot, sm: SM, now: datetime) -> int:
    """Approved orders whose invite links failed: retry with a growing interval."""
    async with sm() as session:
        orders = (await session.scalars(
            select(Order).where(Order.status == OrderStatus.APPROVED, Order.access_granted.is_(False),
                                Order.access_attempts < access_svc.MAX_ACCESS_ATTEMPTS)
        )).all()
        done = 0
        for order in orders:
            wait = timedelta(minutes=2 * max(order.access_attempts, 1))
            if order.access_attempts and order.updated_at > now - wait:
                continue
            if await access_svc.grant_order_access(bot, session, order.id):
                done += 1
        return done


# ------------------------------------------------------------------ subscriptions
async def job_sub_reminders(bot: Bot, sm: SM, now: datetime) -> int:
    """'X days left' reminders. Sent only while still relevant: after downtime a stale
    3-day reminder is skipped if the 1-day window has already started."""
    first = timedelta(days=settings.remind_first_days)
    second = timedelta(days=settings.remind_second_days)
    sent = 0
    async with sm() as session:
        subs = (await session.scalars(
            select(Subscription).where(Subscription.status == SubStatus.ACTIVE,
                                       Subscription.expires_at.is_not(None),
                                       Subscription.expires_at > now,
                                       Subscription.expires_at <= now + first)
        )).all()
        for sub in subs:
            left = sub.expires_at - now
            if left <= second:
                if sub.reminded_second_for == sub.expires_at:
                    continue
                key, days = "remind_second", settings.remind_second_days
                sub.reminded_second_for = sub.expires_at
                sub.reminded_first_for = sub.expires_at
            else:
                if sub.reminded_first_for == sub.expires_at:
                    continue
                key, days = "remind_first", settings.remind_first_days
                sub.reminded_first_for = sub.expires_at
            user = await session.get(User, sub.user_id)
            plan = await session.get(Plan, sub.plan_id)
            if user is None or user.is_blocked:
                continue
            msg = await safe_send(bot, session, user.id,
                                  t(user.language, key, plan=esc(plan.name), days=days,
                                    until=fmt_dt(sub.expires_at, settings.tz)),
                                  reply_markup=renew_kb(user.language, plan.id if plan.is_active else None))
            if msg:
                sent += 1
        await session.commit()
    return sent


async def job_expirations(bot: Bot, sm: SM, now: datetime) -> int:
    """Expired subscriptions (+GRACE_DAYS): remove from channels and notify once."""
    count = 0
    failed_users: list[int] = []
    async with sm() as session:
        subs = (await session.scalars(
            select(Subscription).where(Subscription.status == SubStatus.ACTIVE,
                                       Subscription.expires_at.is_not(None),
                                       Subscription.expires_at <= now - grace())
        )).all()
        for sub in subs:
            sub.status = SubStatus.EXPIRED
            sub.removed_at = now
            await session.flush()
            plan = await session.get(Plan, sub.plan_id)
            channel_ids = {c.id for c in plan.channels}
            _removed, failed = await access_svc.remove_uncovered(bot, session, sub.user_id, channel_ids, now)
            if failed:
                failed_users.append(sub.user_id)
            user = await session.get(User, sub.user_id)
            if user and not user.is_blocked and sub.expired_notified_for != sub.expires_at:
                await safe_send(bot, session, user.id, t(user.language, "sub_expired_user", plan=esc(plan.name)),
                                reply_markup=renew_kb(user.language, plan.id if plan.is_active else None))
            sub.expired_notified_for = sub.expires_at
            await session.commit()
            count += 1
        if failed_users:
            await notify_admins_t(bot, session, "admin_alert_remove_failed", count=len(failed_users),
                                  ids=_ids(failed_users))
    return count


# ------------------------------------------------------------------ reconciliation
@dataclass
class ReconcileChannel:
    title: str
    telegram_count: int | None
    known: int
    unpaid: list[int] = field(default_factory=list)
    no_access: list[int] = field(default_factory=list)
    removed: int = 0


async def reconcile(bot: Bot, sm: SM, now: datetime) -> list[ReconcileChannel]:
    """Compare channel members known to the bot with active subscriptions.

    The Bot API cannot list all channel members; we check every user the bot knows
    (subscribers + people seen via join requests/membership updates) with getChatMember.
    """
    report: list[ReconcileChannel] = []
    async with sm() as session:
        channels = (await session.scalars(select(Channel).where(Channel.is_active.is_(True)))).all()
        for channel in channels:
            sub_users = set((await session.scalars(
                select(Subscription.user_id).join(plan_channels, plan_channels.c.plan_id == Subscription.plan_id)
                .where(plan_channels.c.channel_id == channel.id)
            )).all())
            seen_users = set((await session.scalars(
                select(ChannelMember.user_id).where(ChannelMember.channel_id == channel.id)
            )).all())
            candidates = {u for u in (sub_users | seen_users) if not is_protected(u)}
            try:
                tg_count = await bot.get_chat_member_count(channel.chat_id)
            except Exception:  # noqa: BLE001
                tg_count = None
            item = ReconcileChannel(channel.title, tg_count, len(candidates))
            for uid in sorted(candidates):
                status = await access_svc.get_member_status(bot, channel.chat_id, uid)
                await asyncio.sleep(0.05)  # stay far below API limits
                if status is None:
                    continue
                await access_svc.set_member_status(session, uid, channel.id, status)
                allowed = channel.id in await active_channel_ids(session, uid, now)
                if status in access_svc.MEMBER_STATUSES and not allowed:
                    item.unpaid.append(uid)
                    if settings.auto_remove_unpaid and await access_svc.remove_from_channel(bot, channel.chat_id, uid):
                        item.removed += 1
                        await access_svc.set_member_status(session, uid, channel.id, "left")
                elif status not in access_svc.MEMBER_STATUSES and allowed:
                    item.no_access.append(uid)
            await session.commit()
            report.append(item)
    return report


def _ids(ids: list[int], limit: int = 20) -> str:
    text = ", ".join(f"<code>{i}</code>" for i in ids[:limit])
    return text + (f" … (+{len(ids) - limit})" if len(ids) > limit else "")


async def job_reconcile(bot: Bot, sm: SM, now: datetime) -> int:
    report = await reconcile(bot, sm, now)
    async with sm() as session:
        def render(lang: str) -> str:
            parts = [t(lang, "reconcile_title")]
            for item in report:
                parts.append(t(lang, "reconcile_channel", title=esc(item.title),
                               total=item.telegram_count if item.telegram_count is not None else "?",
                               known=item.known))
                if item.unpaid:
                    parts.append(t(lang, "reconcile_unpaid", count=len(item.unpaid), ids=_ids(item.unpaid)))
                if item.removed:
                    parts.append(t(lang, "reconcile_removed", count=item.removed))
                if item.no_access:
                    parts.append(t(lang, "reconcile_no_access", count=len(item.no_access), ids=_ids(item.no_access)))
                if not item.unpaid and not item.no_access:
                    parts.append(t(lang, "reconcile_ok"))
            if not report:
                parts.append(t(lang, "reconcile_no_channels"))
            if any(i.unpaid for i in report) and not settings.auto_remove_unpaid:
                parts.append(t(lang, "reconcile_auto_off"))
            parts.append(t(lang, "reconcile_limit_note"))
            return "\n".join(parts)

        for admin_id, lang in (await admin_languages(session)).items():
            try:
                await bot.send_message(admin_id, render(lang))
            except Exception as exc:  # noqa: BLE001
                log.warning("Cannot send reconcile report to %s: %s", admin_id, exc)
    return sum(len(i.unpaid) + len(i.no_access) for i in report)


# ------------------------------------------------------------------ backups
async def job_backup(bot: Bot, sm: SM, now: datetime) -> int:
    path = await make_backup()
    if settings.backup_send_to_admin:
        for admin_id in settings.admin_ids:
            try:
                await bot.send_document(admin_id, FSInputFile(path), caption=t(settings.default_language, "backup_caption", name=path.name))
            except Exception as exc:  # noqa: BLE001
                log.warning("Cannot send backup to admin %s: %s", admin_id, exc)
    return 1


# ------------------------------------------------------------------ catch-up
async def recover_join_requests(bot: Bot, sm: SM, now: datetime) -> int:
    """Join requests that arrived while the bot was off for >24h are no longer delivered
    as updates, but they are still pending in the channel: approve them for subscribers."""
    approved = 0
    since = now - timedelta(hours=max(settings.invite_link_ttl_hours, 24) * 2)
    async with sm() as session:
        rows = (await session.execute(
            select(InviteLink.user_id, InviteLink.channel_id).where(InviteLink.created_at >= since,
                                                                     InviteLink.used_at.is_(None)).distinct()
        )).all()
        for user_id, channel_id in rows:
            channel = await session.get(Channel, channel_id)
            if channel is None or not channel.is_active:
                continue
            if channel_id not in await active_channel_ids(session, user_id, now):
                continue
            if await access_svc.try_approve_pending(bot, channel.chat_id, user_id):
                approved += 1
                await access_svc.set_member_status(session, user_id, channel_id, "member")
                for link in (await session.scalars(select(InviteLink).where(
                        InviteLink.user_id == user_id, InviteLink.channel_id == channel_id,
                        InviteLink.used_at.is_(None)))).all():
                    link.used_at = now
        await session.commit()
    return approved


async def mark_interrupted_broadcasts(sm: SM, now: datetime) -> int:
    async with sm() as session:
        running = (await session.scalars(select(Broadcast).where(Broadcast.status == "running"))).all()
        for b in running:
            b.status = "interrupted"
            b.finished_at = now
        await session.commit()
        return len(running)


def daily_due(last_value: str | None, hour: int, now: datetime) -> bool:
    """A daily job is due when local time passed `hour` and it has not run today (local date)."""
    local = to_local(now, settings.tz)
    if local.hour < hour:
        # Not yet time today; but if it never ran for >1 day (bot was off), run anyway.
        if last_value is None:
            return False
        last_date = datetime.fromisoformat(last_value).date()
        return (local.date() - last_date).days >= 2
    return last_value is None or datetime.fromisoformat(last_value).date() < local.date()


async def run_daily(bot: Bot, sm: SM, now: datetime, key: str, hour: int, job) -> bool:
    async with sm() as session:
        last = await kv_get(session, key)
    if not daily_due(last, hour, now):
        return False
    try:
        await job(bot, sm, now)
    finally:
        async with sm() as session:
            await kv_set(session, key, to_local(now, settings.tz).date().isoformat())
            await session.commit()
    return True

