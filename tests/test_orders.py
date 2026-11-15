import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from bot.db.models import Order, OrderStatus, Subscription, User
from bot.services import orders as order_svc
from tests.conftest import NOW, make_user


async def new_pending_order(sm, catalog, user_id=1, plan=None, proof_unique="u1", text=None):
    async with sm() as s:
        user = await s.get(User, user_id)
        draft = await order_svc.create_order(s, user, plan or catalog.month, catalog.method, NOW)
        await s.commit()
        order = draft.order
        await order_svc.request_proof(s, order.id, user_id, NOW)
        assert await order_svc.submit_proof(s, order, "photo", "file1", proof_unique, text, NOW)
        await s.commit()
        return order


async def test_create_order_basic(sm, catalog):
    await make_user(sm, 1)
    async with sm() as s:
        user = await s.get(User, 1)
        draft = await order_svc.create_order(s, user, catalog.month, catalog.method, NOW)
        await s.commit()
        o = draft.order
        assert o.status == OrderStatus.AWAITING_PAYMENT
        assert o.final_amount == 100000 and o.currency == "RUB"
        assert len(o.code) == 5
        assert o.expires_at == NOW + timedelta(hours=24)


async def test_open_orders_limit(sm, catalog):
    await make_user(sm, 1)
    async with sm() as s:
        user = await s.get(User, 1)
        for _ in range(3):
            await order_svc.create_order(s, user, catalog.month, catalog.method, NOW)
        await s.commit()
        with pytest.raises(order_svc.OrderError):
            await order_svc.create_order(s, user, catalog.month, catalog.method, NOW)


async def test_approve_is_idempotent(sm, catalog):
    await make_user(sm, 1)
    order = await new_pending_order(sm, catalog)
    async with sm() as s:
        r1 = await order_svc.approve_order(s, order.id, 999, NOW)
    async with sm() as s:
        r2 = await order_svc.approve_order(s, order.id, 999, NOW)
        r3 = await order_svc.reject_order(s, order.id, 999, "x", NOW)
    assert r1.status == "ok" and r2.status == "already" and r3.status == "already"
    async with sm() as s:
        subs = (await s.scalars(select(Subscription))).all()
        assert len(subs) == 1 and subs[0].expires_at == NOW + timedelta(days=30)  # granted exactly once


async def test_two_admins_click_at_the_same_time(sm, catalog):
    await make_user(sm, 1)
    order = await new_pending_order(sm, catalog)

    async def click(admin_id):
        async with sm() as s:
            return (await order_svc.approve_order(s, order.id, admin_id, NOW)).status

    results = await asyncio.gather(click(998), click(999))
    assert sorted(results) == ["already", "ok"]


async def test_reject_then_resubmit(sm, catalog):
    await make_user(sm, 1)
    order = await new_pending_order(sm, catalog)
    async with sm() as s:
        r = await order_svc.reject_order(s, order.id, 999, "tpl:not_found", NOW)
        assert r.status == "ok"
    async with sm() as s:
        assert await order_svc.request_proof(s, order.id, 1, NOW + timedelta(hours=1)) is not None
        await s.commit()
        waiting = await order_svc.order_waiting_proof(s, 1, NOW + timedelta(hours=1))
        assert waiting.id == order.id
        assert await order_svc.submit_proof(s, waiting, "text", None, None, "TX123456", NOW + timedelta(hours=1))
        await s.commit()
        assert waiting.status == OrderStatus.PENDING_REVIEW


async def test_cancel_only_before_receipt(sm, catalog):
    await make_user(sm, 1)
    async with sm() as s:
        user = await s.get(User, 1)
        o = (await order_svc.create_order(s, user, catalog.month, catalog.method, NOW)).order
        await s.commit()
        assert await order_svc.cancel_order(s, o.id, 2, NOW) is False  # someone else's order
        assert await order_svc.cancel_order(s, o.id, 1, NOW) is True
    pending = await new_pending_order(sm, catalog, proof_unique="u2")
    async with sm() as s:
        assert await order_svc.cancel_order(s, pending.id, 1, NOW) is False


async def test_orders_without_receipt_expire(sm, catalog):
    await make_user(sm, 1)
    async with sm() as s:
        user = await s.get(User, 1)
        o = (await order_svc.create_order(s, user, catalog.month, catalog.method, NOW)).order
        await s.commit()
        assert await order_svc.expire_orders(s, NOW + timedelta(hours=23)) == []
        expired = await order_svc.expire_orders(s, NOW + timedelta(hours=25))
        assert [e.id for e in expired] == [o.id]
        await s.refresh(o)
        assert o.status == OrderStatus.EXPIRED


async def test_duplicate_receipt_detection(sm, catalog):
    await make_user(sm, 1)
    await make_user(sm, 2)
    first = await new_pending_order(sm, catalog, user_id=1, proof_unique="SAME", text="op 1234-5678")
    second = await new_pending_order(sm, catalog, user_id=2, proof_unique="OTHER", text="OP12345678")
    third = await new_pending_order(sm, catalog, user_id=2, proof_unique="SAME", plan=catalog.life)
    async with sm() as s:
        assert await order_svc.find_duplicate_codes(s, await s.get(Order, second.id)) == [first.code]
        assert first.code in await order_svc.find_duplicate_codes(s, await s.get(Order, third.id))


async def test_approve_adds_promo_bonus_days(sm, catalog):
    from bot.db.models import PromoCode

    await make_user(sm, 1)
    async with sm() as s:
        promo = PromoCode(code="BONUS", kind="percent", value=10, bonus_days=5)
        s.add(promo)
        await s.flush()
        user = await s.get(User, 1)
        user.active_promo_id = promo.id
        await s.commit()
    order = await new_pending_order(sm, catalog)
    assert order.final_amount == 90000 and order.bonus_days == 5
    async with sm() as s:
        r = await order_svc.approve_order(s, order.id, 999, NOW)
        assert r.subscription.expires_at == NOW + timedelta(days=35)
        promo = await s.scalar(select(PromoCode).where(PromoCode.code == "BONUS"))
        assert promo.used_count == 1
        assert (await s.get(User, 1)).active_promo_id is None
