"""Orders: creation, receipts, atomic & idempotent approve/reject/cancel, expiry."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import (
    Order,
    OrderStatus,
    PaymentMethodInfo,
    Plan,
    PlanPrice,
    PromoCode,
    Subscription,
    User,
)
from bot.services import promo as promo_svc
from bot.services import referral as ref_svc
from bot.services.common import audit
from bot.services.subscriptions import grant
from bot.utils.text import normalize_proof_text, random_code

log = logging.getLogger(__name__)


class OrderError(Exception):
    """Raised with an i18n key when an order cannot be created."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


async def generate_order_code(session: AsyncSession) -> str:
    for _ in range(50):
        code = random_code(5)
        if not await session.scalar(select(Order.id).where(Order.code == code)):
            return code
    raise RuntimeError("Cannot generate a unique order code")


async def open_orders_count(session: AsyncSession, user_id: int) -> int:
    return int(await session.scalar(
        select(func.count()).select_from(Order).where(
            Order.user_id == user_id,
            Order.status.in_((OrderStatus.AWAITING_PAYMENT, OrderStatus.PENDING_REVIEW)),
        )
    ) or 0)


@dataclass
class OrderDraft:
    order: Order
    promo_ignored: bool = False


async def create_order(session: AsyncSession, user: User, plan: Plan, method: PaymentMethodInfo,
                       now: datetime) -> OrderDraft:
    if not plan.is_active or not method.is_active:
        raise OrderError("order_unavailable")
    price = await session.scalar(
        select(PlanPrice).where(PlanPrice.plan_id == plan.id, PlanPrice.payment_method_id == method.id)
    )
    if price is None:
        raise OrderError("order_unavailable")
    if await open_orders_count(session, user.id) >= settings.max_open_orders_per_user:
        raise OrderError("order_too_many_open")

    base = price.amount
    promo: PromoCode | None = None
    promo_ignored = False
    if user.active_promo_id:
        candidate = None
        stored = await session.get(PromoCode, user.active_promo_id)
        if stored is not None:
            candidate, _err = await promo_svc.validate_promo(session, stored.code, user.id, now)
        if candidate is None:
            user.active_promo_id = None
            promo_ignored = True
        elif not promo_svc.promo_applies_to_plan(candidate, plan):
            promo_ignored = True
        else:
            promo = candidate

    promo_discount = promo_svc.calc_discount(promo, base, method.currency)
    ref_discount = 0
    if await ref_svc.referral_discount_applies(session, user):
        ref_discount = promo_svc.calc_percent_discount(base, settings.ref_discount_percent)
    # Discounts do not stack: the larger one wins. Promo bonus days always apply.
    use_referral = ref_discount > promo_discount
    discount = max(promo_discount, ref_discount)

    order = Order(
        code=await generate_order_code(session),
        user_id=user.id,
        plan_id=plan.id,
        payment_method_id=method.id,
        provider=method.provider,
        currency=method.currency,
        base_amount=base,
        discount_amount=discount,
        final_amount=base - discount,
        promo_id=promo.id if promo else None,
        referral_discount=use_referral,
        bonus_days=promo.bonus_days if promo else 0,
        status=OrderStatus.AWAITING_PAYMENT,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=settings.order_ttl_hours),
    )
    session.add(order)
    await session.flush()
    return OrderDraft(order, promo_ignored)


def can_resubmit(order: Order, now: datetime) -> bool:
    """A rejected order may get a new receipt within ORDER_TTL_HOURS after rejection."""
    return (order.status == OrderStatus.REJECTED and order.reviewed_at is not None
            and order.reviewed_at + timedelta(hours=settings.order_ttl_hours) > now)


async def request_proof(session: AsyncSession, order_id: int, user_id: int, now: datetime) -> Order | None:
    """The buyer pressed "I paid": remember that the next photo/file/text is the receipt."""
    order = await session.get(Order, order_id)
    if order is None or order.user_id != user_id:
        return None
    if (order.status == OrderStatus.AWAITING_PAYMENT and order.expires_at > now) or can_resubmit(order, now):
        order.proof_requested = True
        order.updated_at = now
        return order
    return None


async def order_waiting_proof(session: AsyncSession, user_id: int, now: datetime) -> Order | None:
    orders = (await session.scalars(
        select(Order).where(
            Order.user_id == user_id, Order.proof_requested.is_(True),
            Order.status.in_((OrderStatus.AWAITING_PAYMENT, OrderStatus.REJECTED)),
        ).order_by(Order.updated_at.desc())
    )).all()
    for order in orders:
        if order.status == OrderStatus.AWAITING_PAYMENT and order.expires_at > now:
            return order
        if can_resubmit(order, now):
            return order
    return None


async def submit_proof(session: AsyncSession, order: Order, kind: str, file_id: str | None,
                       file_unique_id: str | None, text: str | None, now: datetime) -> bool:
    """Atomically move the order to pending_review. False if it was already moved."""
    res = await session.execute(
        update(Order)
        .where(Order.id == order.id, Order.status.in_((OrderStatus.AWAITING_PAYMENT, OrderStatus.REJECTED)))
        .values(status=OrderStatus.PENDING_REVIEW, proof_type=kind, proof_file_id=file_id,
                proof_file_unique_id=file_unique_id, proof_text=(text or None),
                proof_text_norm=normalize_proof_text(text), proof_submitted_at=now,
                proof_requested=False, reviewed_by=None, reviewed_at=None, reject_reason=None,
                updated_at=now)
    )
    if res.rowcount != 1:
        return False
    await session.refresh(order)
    return True


async def find_duplicate_codes(session: AsyncSession, order: Order) -> list[str]:
    """Other orders that used the same receipt file or the same operation id."""
    conds = []
    if order.proof_file_unique_id:
        conds.append(Order.proof_file_unique_id == order.proof_file_unique_id)
    if order.proof_text_norm:
        conds.append(Order.proof_text_norm == order.proof_text_norm)
    if not conds:
        return []
    rows = await session.scalars(select(Order.code).where(Order.id != order.id, or_(*conds)).limit(10))
    return list(rows)


@dataclass
class ApproveResult:
    status: str  # ok | already | not_found
    order: Order | None = None
    subscription: Subscription | None = None
    reward: ref_svc.RewardResult | None = None
    days_granted: int | None = None


async def approve_order(session: AsyncSession, order_id: int, admin_id: int | None, now: datetime) -> ApproveResult:
    """Atomic + idempotent: only one caller can move pending_review -> approved.

    Order status, subscription, promo usage and referral reward are committed in ONE transaction.
    Access (invite links) is granted afterwards and retried by the scheduler if it fails.
    """
    res = await session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status == OrderStatus.PENDING_REVIEW)
        .values(status=OrderStatus.APPROVED, reviewed_by=admin_id, reviewed_at=now, updated_at=now)
    )
    if res.rowcount != 1:
        await session.rollback()
        order = await session.get(Order, order_id)
        return ApproveResult("not_found" if order is None else "already", order)
    try:
        order = await session.get(Order, order_id)
        await session.refresh(order)
        user = await session.get(User, order.user_id)
        plan = await session.get(Plan, order.plan_id)
        days: int | None = None
        if plan.duration_days is not None:
            days = plan.duration_days + order.bonus_days + user.bonus_days_balance
            user.bonus_days_balance = 0
        sub = await grant(session, user.id, plan, days, now, source="order")
        if order.promo_id:
            await promo_svc.record_usage(session, order.promo_id, user.id, order.id)
            if user.active_promo_id == order.promo_id:
                user.active_promo_id = None
        reward = await ref_svc.reward_referrer(session, user, order, now)
        await audit(session, admin_id, "order_approve", f"order:{order.code}",
                    f"user={user.id} plan={plan.id} amount={order.final_amount} {order.currency}")
        await session.commit()
        return ApproveResult("ok", order, sub, reward, days)
    except Exception:
        await session.rollback()
        log.exception("Approve of order %s failed, rolled back", order_id)
        raise


async def reject_order(session: AsyncSession, order_id: int, admin_id: int | None, reason: str,
                       now: datetime) -> ApproveResult:
    res = await session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status == OrderStatus.PENDING_REVIEW)
        .values(status=OrderStatus.REJECTED, reviewed_by=admin_id, reviewed_at=now,
                reject_reason=reason[:1000], proof_requested=False, updated_at=now)
    )
    if res.rowcount != 1:
        await session.rollback()
        order = await session.get(Order, order_id)
        return ApproveResult("not_found" if order is None else "already", order)
    order = await session.get(Order, order_id)
    await session.refresh(order)
    await audit(session, admin_id, "order_reject", f"order:{order.code}", reason[:300])
    await session.commit()
    return ApproveResult("ok", order)


async def cancel_order(session: AsyncSession, order_id: int, user_id: int, now: datetime) -> bool:
    res = await session.execute(
        update(Order)
        .where(Order.id == order_id, Order.user_id == user_id, Order.status == OrderStatus.AWAITING_PAYMENT)
        .values(status=OrderStatus.CANCELLED, updated_at=now, proof_requested=False)
    )
    await session.commit()
    return res.rowcount == 1


async def expire_orders(session: AsyncSession, now: datetime) -> list[Order]:
    """awaiting_payment orders without a receipt for ORDER_TTL_HOURS -> expired."""
    orders = list((await session.scalars(
        select(Order).where(Order.status == OrderStatus.AWAITING_PAYMENT, Order.expires_at <= now)
    )).all())
    expired = []
    for order in orders:
        res = await session.execute(
            update(Order).where(Order.id == order.id, Order.status == OrderStatus.AWAITING_PAYMENT)
            .values(status=OrderStatus.EXPIRED, updated_at=now, proof_requested=False)
        )
        if res.rowcount == 1:
            expired.append(order)
    await session.commit()
    return expired
