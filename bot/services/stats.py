"""Statistics and CSV export."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from bot.config import settings
from bot.db.models import (
    Channel,
    Order,
    OrderStatus,
    PaymentMethodInfo,
    Plan,
    PromoCode,
    ReferralReward,
    SubStatus,
    Subscription,
    User,
    plan_channels,
)
from bot.services.subscriptions import active_filter
from bot.utils.money import fmt_amount
from bot.utils.timeutil import fmt_dt, local_today_start_utc


@dataclass
class Period:
    key: str
    start: datetime


def periods(now: datetime) -> list[Period]:
    today = local_today_start_utc(now, settings.tz)
    return [Period("day", today), Period("week", now - timedelta(days=7)), Period("month", now - timedelta(days=30))]


async def active_by_channel(session: AsyncSession, now: datetime) -> list[tuple[str, int]]:
    rows = await session.execute(
        select(Channel.title, func.count(func.distinct(Subscription.user_id)))
        .select_from(Channel)
        .join(plan_channels, plan_channels.c.channel_id == Channel.id)
        .join(Subscription, and_(Subscription.plan_id == plan_channels.c.plan_id, active_filter(now)))
        .group_by(Channel.id).order_by(Channel.id)
    )
    return [(title, cnt) for title, cnt in rows.all()]


async def total_active(session: AsyncSession, now: datetime) -> int:
    return int(await session.scalar(select(func.count(func.distinct(Subscription.user_id))).where(active_filter(now))) or 0)


async def revenue(session: AsyncSession, start: datetime) -> list[tuple[str, str, int, int]]:
    rows = await session.execute(
        select(PaymentMethodInfo.name, Order.currency, func.sum(Order.final_amount), func.count(Order.id))
        .join(PaymentMethodInfo, PaymentMethodInfo.id == Order.payment_method_id)
        .where(Order.status == OrderStatus.APPROVED, Order.refunded.is_(False), Order.reviewed_at >= start)
        .group_by(PaymentMethodInfo.name, Order.currency)
    )
    return [(m, c, int(s or 0), int(n)) for m, c, s, n in rows.all()]


async def new_and_renewed(session: AsyncSession, start: datetime) -> tuple[int, int]:
    approved = select(Order.user_id, Order.plan_id, Order.reviewed_at).where(
        Order.status == OrderStatus.APPROVED).subquery()
    first = select(approved.c.user_id, func.min(approved.c.reviewed_at).label("first_at")).group_by(
        approved.c.user_id).subquery()
    new = await session.scalar(select(func.count()).select_from(first).where(first.c.first_at >= start))
    earlier = aliased(Order)
    renewed = await session.scalar(
        select(func.count(Order.id)).where(
            Order.status == OrderStatus.APPROVED, Order.reviewed_at >= start,
            select(earlier.id).where(earlier.user_id == Order.user_id, earlier.plan_id == Order.plan_id,
                                     earlier.status == OrderStatus.APPROVED,
                                     earlier.reviewed_at < Order.reviewed_at).exists(),
        )
    )
    return int(new or 0), int(renewed or 0)


async def churned(session: AsyncSession, start: datetime, now: datetime) -> int:
    return int(await session.scalar(
        select(func.count(func.distinct(Subscription.user_id))).where(
            Subscription.status == SubStatus.EXPIRED, Subscription.expires_at >= start, Subscription.expires_at <= now)
    ) or 0)


async def promo_stats(session: AsyncSession, limit: int = 10) -> list[tuple[str, int, list[tuple[str, int]]]]:
    promos = (await session.scalars(select(PromoCode).order_by(PromoCode.used_count.desc()).limit(limit))).all()
    result = []
    for promo in promos:
        rows = await session.execute(
            select(Order.currency, func.sum(Order.final_amount)).where(
                Order.promo_id == promo.id, Order.status == OrderStatus.APPROVED, Order.refunded.is_(False)
            ).group_by(Order.currency)
        )
        result.append((promo.code, promo.used_count, [(c, int(s or 0)) for c, s in rows.all()]))
    return result


async def referral_stats(session: AsyncSession) -> tuple[int, int, list[tuple[str, int]]]:
    referred = int(await session.scalar(select(func.count()).select_from(User).where(User.referred_by.is_not(None))) or 0)
    rewards = int(await session.scalar(select(func.count()).select_from(ReferralReward)) or 0)
    rows = await session.execute(
        select(Order.currency, func.sum(Order.final_amount)).join(User, User.id == Order.user_id).where(
            User.referred_by.is_not(None), Order.status == OrderStatus.APPROVED, Order.refunded.is_(False)
        ).group_by(Order.currency)
    )
    return referred, rewards, [(c, int(s or 0)) for c, s in rows.all()]


def money_list(items: list[tuple[str, int]]) -> str:
    return ", ".join(fmt_amount(s, c) for c, s in items) or "0"


# ------------------------------------------------------------------ CSV export
def _csv(rows: list[list[object]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerows(rows)
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM so Excel opens UTF-8 correctly


async def export_orders(session: AsyncSession) -> bytes:
    tz = settings.tz
    rows: list[list[object]] = [[
        "code", "status", "user_id", "username", "plan", "method", "currency", "base_amount", "discount",
        "final_amount", "promo", "bonus_days", "created", "proof_submitted", "reviewed", "reviewed_by",
        "refunded", "reject_reason",
    ]]
    q = (select(Order, User, Plan, PaymentMethodInfo, PromoCode)
         .join(User, User.id == Order.user_id).join(Plan, Plan.id == Order.plan_id)
         .join(PaymentMethodInfo, PaymentMethodInfo.id == Order.payment_method_id)
         .outerjoin(PromoCode, PromoCode.id == Order.promo_id).order_by(Order.id))
    for order, user, plan, method, promo in (await session.execute(q)).all():
        rows.append([
            order.code, order.status, user.id, user.username or "", plan.name, method.name, order.currency,
            fmt_amount(order.base_amount), fmt_amount(order.discount_amount), fmt_amount(order.final_amount),
            promo.code if promo else "", order.bonus_days, fmt_dt(order.created_at, tz),
            fmt_dt(order.proof_submitted_at, tz), fmt_dt(order.reviewed_at, tz), order.reviewed_by or "",
            "yes" if order.refunded else "", order.reject_reason or "",
        ])
    return _csv(rows)


async def export_subscribers(session: AsyncSession, now: datetime) -> bytes:
    tz = settings.tz
    rows: list[list[object]] = [[
        "user_id", "username", "first_name", "language", "plan", "status", "active_now", "started", "expires",
        "referred_by", "blocked_bot",
    ]]
    q = (select(Subscription, User, Plan).join(User, User.id == Subscription.user_id)
         .join(Plan, Plan.id == Subscription.plan_id).order_by(User.id))
    from bot.services.subscriptions import is_sub_active

    for sub, user, plan in (await session.execute(q)).all():
        rows.append([
            user.id, user.username or "", user.first_name or "", user.language, plan.name, sub.status,
            "yes" if is_sub_active(sub, now) else "no", fmt_dt(sub.started_at, tz),
            "lifetime" if sub.expires_at is None else fmt_dt(sub.expires_at, tz),
            user.referred_by or "", "yes" if user.is_blocked else "",
        ])
    return _csv(rows)
