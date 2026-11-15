"""Subscriptions: expiry maths, granting/extending, access checks."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Channel, Plan, SubStatus, Subscription, plan_channels


def compute_new_expiry(current_expires: datetime | None, current_active: bool, now: datetime,
                       days: int | None) -> datetime | None:
    """Return the new expiry date.

    * ``days is None``  -> lifetime (None).
    * active lifetime   -> stays lifetime.
    * still active      -> days are added to the *current end*, not to today.
    * expired/inactive  -> days are counted from now.
    """
    if days is None:
        return None
    if current_active and current_expires is None:
        return None
    if current_active and current_expires is not None and current_expires > now:
        return current_expires + timedelta(days=days)
    return now + timedelta(days=days)


def grace() -> timedelta:
    return timedelta(days=settings.grace_days)


def is_sub_active(sub: Subscription, now: datetime) -> bool:
    if sub.status != SubStatus.ACTIVE:
        return False
    return sub.expires_at is None or sub.expires_at + grace() > now


def active_filter(now: datetime):
    return (Subscription.status == SubStatus.ACTIVE) & or_(
        Subscription.expires_at.is_(None), Subscription.expires_at > now - grace()
    )


async def grant(session: AsyncSession, user_id: int, plan: Plan, days: int | None, now: datetime,
                source: str = "order") -> Subscription:
    """Create or extend the user's subscription to a plan.

    ``days`` None means lifetime. Bonus days must be already included by the caller.
    """
    sub = await session.scalar(
        select(Subscription).where(Subscription.user_id == user_id, Subscription.plan_id == plan.id)
    )
    if sub is None:
        sub = Subscription(user_id=user_id, plan_id=plan.id, status=SubStatus.ACTIVE, started_at=now,
                           expires_at=compute_new_expiry(None, False, now, days), source=source)
        session.add(sub)
    else:
        active = is_sub_active(sub, now)
        sub.expires_at = compute_new_expiry(sub.expires_at, active, now, days)
        if not active:
            sub.started_at = now
        sub.status = SubStatus.ACTIVE
        sub.removed_at = None
        sub.source = source
    await session.flush()
    return sub


async def add_days_to_best_sub(session: AsyncSession, user_id: int, days: int, now: datetime) -> Subscription | None:
    """Add bonus days to the user's active non-lifetime subscription ending soonest-last."""
    subs = (await session.scalars(
        select(Subscription).where(Subscription.user_id == user_id, active_filter(now),
                                   Subscription.expires_at.is_not(None))
        .order_by(Subscription.expires_at.desc())
    )).all()
    if not subs:
        return None
    sub = subs[0]
    sub.expires_at = compute_new_expiry(sub.expires_at, True, now, days)
    await session.flush()
    return sub


async def active_channel_ids(session: AsyncSession, user_id: int, now: datetime) -> set[int]:
    rows = await session.execute(
        select(Channel.id)
        .join(plan_channels, plan_channels.c.channel_id == Channel.id)
        .join(Subscription, Subscription.plan_id == plan_channels.c.plan_id)
        .where(Subscription.user_id == user_id, active_filter(now), Channel.is_active.is_(True))
    )
    return {r[0] for r in rows}


async def has_access(session: AsyncSession, user_id: int, channel_id: int, now: datetime) -> bool:
    return channel_id in await active_channel_ids(session, user_id, now)


async def user_subscriptions(session: AsyncSession, user_id: int) -> list[tuple[Subscription, Plan]]:
    rows = await session.execute(
        select(Subscription, Plan).join(Plan, Plan.id == Subscription.plan_id)
        .where(Subscription.user_id == user_id).order_by(Subscription.expires_at.desc())
    )
    return [(s, p) for s, p in rows.all()]


async def active_user_ids(session: AsyncSession, now: datetime) -> set[int]:
    rows = await session.execute(select(Subscription.user_id).where(active_filter(now)).distinct())
    return {r[0] for r in rows}
