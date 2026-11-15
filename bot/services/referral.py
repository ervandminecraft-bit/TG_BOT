"""Referral program: attaching referrers and rewarding them after the first approved order."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Order, OrderStatus, ReferralReward, User
from bot.services.subscriptions import add_days_to_best_sub
from bot.utils.text import random_code


async def generate_ref_code(session: AsyncSession) -> str:
    while True:
        code = random_code(8)
        if not await session.scalar(select(User.id).where(User.ref_code == code)):
            return code


async def attach_referrer(session: AsyncSession, user: User, ref_code: str, is_new: bool) -> bool:
    """Link a NEW user to the owner of ref_code. Protects against self- and circular referrals."""
    if not is_new or user.referred_by is not None:
        return False
    referrer = await session.scalar(select(User).where(User.ref_code == ref_code))
    if referrer is None or referrer.id == user.id or referrer.referred_by == user.id:
        return False
    user.referred_by = referrer.id
    return True


async def approved_orders_count(session: AsyncSession, user_id: int, exclude_order_id: int | None = None) -> int:
    q = select(func.count()).select_from(Order).where(Order.user_id == user_id, Order.status == OrderStatus.APPROVED)
    if exclude_order_id is not None:
        q = q.where(Order.id != exclude_order_id)
    return int(await session.scalar(q) or 0)


async def referral_discount_applies(session: AsyncSession, user: User) -> bool:
    if user.referred_by is None or settings.ref_discount_percent <= 0:
        return False
    return await approved_orders_count(session, user.id) == 0


@dataclass
class RewardResult:
    referrer_id: int
    days: int
    applied_to: str  # subscription | balance


async def reward_referrer(session: AsyncSession, referred: User, order: Order, now: datetime) -> RewardResult | None:
    """Reward the referrer once, after the referred user's FIRST approved order."""
    if referred.referred_by is None or settings.ref_bonus_days <= 0:
        return None
    if await approved_orders_count(session, referred.id, exclude_order_id=order.id) > 0:
        return None
    if await session.scalar(select(ReferralReward.id).where(ReferralReward.referred_id == referred.id)):
        return None
    referrer = await session.get(User, referred.referred_by)
    if referrer is None:
        return None
    month_ago = now - timedelta(days=30)
    recent = await session.scalar(
        select(func.count()).select_from(ReferralReward)
        .where(ReferralReward.referrer_id == referrer.id, ReferralReward.created_at > month_ago)
    )
    if settings.ref_max_rewards_per_month and (recent or 0) >= settings.ref_max_rewards_per_month:
        return None
    days = settings.ref_bonus_days
    sub = await add_days_to_best_sub(session, referrer.id, days, now)
    applied_to = "subscription"
    if sub is None:
        referrer.bonus_days_balance += days
        applied_to = "balance"
    session.add(ReferralReward(referrer_id=referrer.id, referred_id=referred.id, order_id=order.id,
                               bonus_days=days, applied_to=applied_to, created_at=now))
    return RewardResult(referrer.id, days, applied_to)
