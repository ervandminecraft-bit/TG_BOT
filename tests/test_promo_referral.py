from datetime import timedelta

from bot.db.models import PromoCode, ReferralReward, Subscription, User
from bot.services import orders as order_svc
from bot.services import promo as promo_svc
from bot.services import referral as ref_svc
from bot.services.subscriptions import grant
from tests.conftest import NOW, make_user
from tests.test_orders import new_pending_order


async def add_promo(sm, **kw) -> PromoCode:
    async with sm() as s:
        p = PromoCode(**{"code": "SALE", "kind": "percent", "value": 20, **kw})
        s.add(p)
        await s.commit()
        return p


async def test_promo_validation(sm, catalog):
    await make_user(sm, 1)
    await add_promo(sm, code="OLD", valid_until=NOW - timedelta(days=1))
    await add_promo(sm, code="FULL", max_uses=1, used_count=1)
    await add_promo(sm, code="OFF", is_active=False)
    await add_promo(sm, code="GOOD")
    async with sm() as s:
        assert (await promo_svc.validate_promo(s, "nope", 1, NOW))[1] == "promo_not_found"
        assert (await promo_svc.validate_promo(s, "old", 1, NOW))[1] == "promo_expired"
        assert (await promo_svc.validate_promo(s, "FULL", 1, NOW))[1] == "promo_exhausted"
        assert (await promo_svc.validate_promo(s, "OFF", 1, NOW))[1] == "promo_not_found"
        promo, err = await promo_svc.validate_promo(s, " good ", 1, NOW)
        assert err is None and promo.code == "GOOD"


def test_discount_maths():
    pct = PromoCode(code="A", kind="percent", value=15)
    fixed = PromoCode(code="B", kind="fixed", value=30000, currency="RUB")
    assert promo_svc.calc_discount(pct, 100000, "RUB") == 15000
    assert promo_svc.calc_discount(fixed, 100000, "RUB") == 30000
    assert promo_svc.calc_discount(fixed, 100000, "USD") == 0  # other currency
    assert promo_svc.calc_discount(fixed, 20000, "RUB") == 20000  # never below zero


async def test_promo_once_per_user(sm, catalog):
    await make_user(sm, 1)
    promo = await add_promo(sm)
    async with sm() as s:
        (await s.get(User, 1)).active_promo_id = promo.id
        await s.commit()
    order = await new_pending_order(sm, catalog)
    async with sm() as s:
        await order_svc.approve_order(s, order.id, 999, NOW)
    async with sm() as s:
        assert (await promo_svc.validate_promo(s, "SALE", 1, NOW))[1] == "promo_already_used"


async def test_promo_plan_restriction_ignored_for_other_plan(sm, catalog):
    await make_user(sm, 1)
    async with sm() as s:
        life = await s.get(type(catalog.life), catalog.life.id)
        promo = PromoCode(code="LIFEONLY", kind="percent", value=50, plans=[life])
        s.add(promo)
        await s.flush()
        user = await s.get(User, 1)
        user.active_promo_id = promo.id
        await s.commit()
        draft = await order_svc.create_order(s, user, catalog.month, catalog.method, NOW)
        assert draft.promo_ignored and draft.order.discount_amount == 0
        draft2 = await order_svc.create_order(s, user, catalog.life, catalog.method, NOW)
        assert draft2.order.discount_amount == 250000


async def test_referral_attach_protection(sm):
    referrer = await make_user(sm, 1)
    async with sm() as s:
        newbie = User(id=2, language="ru", ref_code="R2", created_at=NOW)
        s.add(newbie)
        assert not await ref_svc.attach_referrer(s, newbie, "R2", is_new=True)  # self-referral
        assert not await ref_svc.attach_referrer(s, newbie, referrer.ref_code, is_new=False)  # old user
        assert await ref_svc.attach_referrer(s, newbie, referrer.ref_code, is_new=True)
        await s.commit()
        r = await s.get(User, 1)
        assert not await ref_svc.attach_referrer(s, r, "R2", is_new=True)  # circular


async def test_referral_discount_and_reward_once(sm, catalog):
    await make_user(sm, 1)
    await make_user(sm, 2, referred_by=1)
    first = await new_pending_order(sm, catalog, user_id=2)
    assert first.discount_amount == 10000 and first.referral_discount  # 10% for the invited friend
    async with sm() as s:
        r = await order_svc.approve_order(s, first.id, 999, NOW)
        assert r.reward is not None and r.reward.applied_to == "balance"  # referrer has no active sub
        assert (await s.get(User, 1)).bonus_days_balance == 7
    second = await new_pending_order(sm, catalog, user_id=2, proof_unique="u9")
    assert second.discount_amount == 0  # discount only for the first order
    async with sm() as s:
        r2 = await order_svc.approve_order(s, second.id, 999, NOW)
        assert r2.reward is None  # reward only once
        rewards = (await s.execute(ReferralReward.__table__.select())).all()
        assert len(rewards) == 1


async def test_referral_reward_extends_active_subscription(sm, catalog):
    await make_user(sm, 1)
    await make_user(sm, 2, referred_by=1)
    async with sm() as s:
        sub = await grant(s, 1, catalog.month, 30, NOW)
        await s.commit()
        end = sub.expires_at
    order = await new_pending_order(sm, catalog, user_id=2)
    async with sm() as s:
        r = await order_svc.approve_order(s, order.id, 999, NOW)
        assert r.reward.applied_to == "subscription"
        fresh = await s.get(Subscription, sub.id)
        assert fresh.expires_at == end + timedelta(days=7)


async def test_bonus_balance_used_on_next_payment(sm, catalog):
    await make_user(sm, 1, bonus_days_balance=7)
    order = await new_pending_order(sm, catalog, user_id=1)
    async with sm() as s:
        r = await order_svc.approve_order(s, order.id, 999, NOW)
        assert r.subscription.expires_at == NOW + timedelta(days=37)
        assert (await s.get(User, 1)).bonus_days_balance == 0
