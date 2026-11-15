"""Promo codes: validation and discount calculation."""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Order, OrderStatus, Plan, PromoCode, PromoUsage

CODE_RE = re.compile(r"^[A-Z0-9_-]{3,32}$")


def normalize_code(text: str) -> str:
    return text.strip().upper()


async def user_used_promo(session: AsyncSession, promo_id: int, user_id: int, exclude_order_id: int | None = None) -> bool:
    used = await session.scalar(
        select(func.count()).select_from(PromoUsage).where(PromoUsage.promo_id == promo_id, PromoUsage.user_id == user_id)
    )
    if used:
        return True
    q = select(func.count()).select_from(Order).where(
        Order.promo_id == promo_id, Order.user_id == user_id,
        Order.status.in_((OrderStatus.AWAITING_PAYMENT, OrderStatus.PENDING_REVIEW)),
    )
    if exclude_order_id:
        q = q.where(Order.id != exclude_order_id)
    return bool(await session.scalar(q))


async def validate_promo(session: AsyncSession, code_text: str, user_id: int, now: datetime) -> tuple[PromoCode | None, str | None]:
    """Return (promo, None) if usable, else (None, i18n error key)."""
    code = normalize_code(code_text)
    if not CODE_RE.match(code):
        return None, "promo_not_found"
    promo = await session.scalar(select(PromoCode).where(PromoCode.code == code))
    if promo is None or not promo.is_active:
        return None, "promo_not_found"
    if promo.valid_until is not None and promo.valid_until < now:
        return None, "promo_expired"
    if promo.max_uses is not None and promo.used_count >= promo.max_uses:
        return None, "promo_exhausted"
    if promo.once_per_user and await user_used_promo(session, promo.id, user_id):
        return None, "promo_already_used"
    return promo, None


def promo_applies_to_plan(promo: PromoCode, plan: Plan) -> bool:
    return not promo.plans or any(p.id == plan.id for p in promo.plans)


def calc_discount(promo: PromoCode | None, base_amount: int, currency: str) -> int:
    if promo is None or base_amount <= 0:
        return 0
    if promo.kind == "percent":
        discount = base_amount * max(0, min(promo.value, 100)) // 100
    elif promo.kind == "fixed":
        discount = promo.value if (promo.currency or "").upper() == currency.upper() else 0
    else:
        discount = 0
    return max(0, min(discount, base_amount))


def calc_percent_discount(base_amount: int, percent: int) -> int:
    return max(0, min(base_amount * max(0, min(percent, 100)) // 100, base_amount))


async def record_usage(session: AsyncSession, promo_id: int, user_id: int, order_id: int) -> None:
    exists = await session.scalar(select(PromoUsage.id).where(PromoUsage.order_id == order_id))
    if exists:
        return
    session.add(PromoUsage(promo_id=promo_id, user_id=user_id, order_id=order_id))
    promo = await session.get(PromoCode, promo_id)
    if promo:
        promo.used_count += 1
