from datetime import timedelta

from bot.db.models import SubStatus
from bot.services.subscriptions import active_channel_ids, compute_new_expiry, grant, has_access, is_sub_active
from tests.conftest import NOW, make_user


def test_new_subscription_counts_from_now():
    assert compute_new_expiry(None, False, NOW, 30) == NOW + timedelta(days=30)


def test_renewal_before_end_adds_to_current_end():
    current = NOW + timedelta(days=5)
    assert compute_new_expiry(current, True, NOW, 30) == current + timedelta(days=30)


def test_renewal_after_end_counts_from_now():
    current = NOW - timedelta(days=3)
    assert compute_new_expiry(current, False, NOW, 30) == NOW + timedelta(days=30)


def test_lifetime_plan_and_lifetime_stays_lifetime():
    assert compute_new_expiry(NOW + timedelta(days=5), True, NOW, None) is None
    assert compute_new_expiry(None, True, NOW, 30) is None  # active lifetime is never shortened


async def test_grant_creates_then_extends(sm, catalog):
    await make_user(sm, 1)
    async with sm() as s:
        sub = await grant(s, 1, catalog.month, 30, NOW)
        await s.commit()
        assert sub.expires_at == NOW + timedelta(days=30)
        sub2 = await grant(s, 1, catalog.month, 30, NOW + timedelta(days=10))
        await s.commit()
        assert sub2.id == sub.id
        assert sub2.expires_at == NOW + timedelta(days=60)  # added to the current end


async def test_grant_after_expiry_restarts(sm, catalog):
    await make_user(sm, 1)
    async with sm() as s:
        sub = await grant(s, 1, catalog.month, 30, NOW - timedelta(days=40))
        sub.status = SubStatus.EXPIRED
        await s.commit()
        sub = await grant(s, 1, catalog.month, 30, NOW)
        await s.commit()
        assert sub.status == SubStatus.ACTIVE
        assert sub.expires_at == NOW + timedelta(days=30)


async def test_access_per_channel(sm, catalog):
    await make_user(sm, 1)
    async with sm() as s:
        await grant(s, 1, catalog.life, None, NOW)
        await s.commit()
        assert await active_channel_ids(s, 1, NOW) == {catalog.ch_a.id}
        assert await has_access(s, 1, catalog.ch_a.id, NOW)
        assert not await has_access(s, 1, catalog.ch_b.id, NOW)


async def test_grace_days(sm, catalog, monkeypatch):
    from bot.config import settings

    await make_user(sm, 1)
    async with sm() as s:
        sub = await grant(s, 1, catalog.month, 1, NOW - timedelta(days=2))
        await s.commit()
        assert not is_sub_active(sub, NOW)
        monkeypatch.setattr(settings, "grace_days", 3)
        assert is_sub_active(sub, NOW)
