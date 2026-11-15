from datetime import datetime, timedelta

from sqlalchemy import select

from bot.config import settings
from bot.db.models import InviteLink, Order, OrderStatus, SubStatus, Subscription, User
from bot.scheduler import jobs
from bot.scheduler.runner import catch_up
from bot.services import orders as order_svc
from bot.services.common import kv_set
from bot.services.subscriptions import grant
from tests.conftest import NOW, make_user


async def sub_ending_in(sm, catalog, user_id, delta, plan=None):
    async with sm() as s:
        sub = await grant(s, user_id, plan or catalog.month, 30, NOW)
        sub.expires_at = NOW + delta
        await s.commit()
        return sub


async def test_first_reminder_sent_once(sm, bot, catalog):
    await make_user(sm, 1)
    await sub_ending_in(sm, catalog, 1, timedelta(days=2, hours=12))
    assert await jobs.job_sub_reminders(bot, sm, NOW) == 1
    assert await jobs.job_sub_reminders(bot, sm, NOW + timedelta(minutes=1)) == 0
    assert len(bot.sent_to(1)) == 1


async def test_stale_reminder_skipped_after_downtime(sm, bot, catalog):
    """The bot was off during the 3-day window: only the 1-day reminder is sent, not both."""
    await make_user(sm, 1)
    await sub_ending_in(sm, catalog, 1, timedelta(hours=12))
    assert await jobs.job_sub_reminders(bot, sm, NOW) == 1
    assert await jobs.job_sub_reminders(bot, sm, NOW) == 0
    assert len(bot.sent_to(1)) == 1
    assert "завтра" in bot.sent_to(1)[0]


async def test_no_reminders_for_lifetime_or_far_or_expired(sm, bot, catalog):
    await make_user(sm, 1)
    await make_user(sm, 2)
    await make_user(sm, 3)
    async with sm() as s:
        await grant(s, 1, catalog.life, None, NOW)
        await s.commit()
    await sub_ending_in(sm, catalog, 2, timedelta(days=10))
    await sub_ending_in(sm, catalog, 3, -timedelta(hours=1))
    assert await jobs.job_sub_reminders(bot, sm, NOW) == 0


async def test_renewal_reenables_reminders(sm, bot, catalog):
    await make_user(sm, 1)
    await sub_ending_in(sm, catalog, 1, timedelta(hours=12))
    await jobs.job_sub_reminders(bot, sm, NOW)
    async with sm() as s:
        await grant(s, 1, catalog.month, 30, NOW)  # renewed: +30 days from current end
        await s.commit()
    later = NOW + timedelta(days=29, hours=10)  # ~1 day before the new end
    assert await jobs.job_sub_reminders(bot, sm, later) == 1


async def test_expiration_removes_and_notifies_once(sm, bot, catalog):
    await make_user(sm, 1)
    await sub_ending_in(sm, catalog, 1, -timedelta(days=5))  # expired while the bot was off
    bot.statuses[(-1001, 1)] = "member"
    assert await jobs.job_expirations(bot, sm, NOW) == 1
    assert bot.count("ban_chat_member") == 2 and bot.count("unban_chat_member") == 2
    assert all(kw.get("only_if_banned") for n, kw in bot.calls if n == "unban_chat_member")
    assert len(bot.sent_to(1)) == 1
    assert await jobs.job_expirations(bot, sm, NOW) == 0
    async with sm() as s:
        assert (await s.scalar(select(Subscription))).status == SubStatus.EXPIRED


async def test_removal_failure_alerts_admin(sm, bot, catalog):
    await make_user(sm, 1)
    await sub_ending_in(sm, catalog, 1, -timedelta(hours=1))
    bot.fail_ban = True
    assert await jobs.job_expirations(bot, sm, NOW) == 1
    assert any("Не удалось удалить" in t for t in bot.sent_to(999))


async def test_expiration_keeps_channel_covered_by_other_plan(sm, bot, catalog):
    await make_user(sm, 1)
    await sub_ending_in(sm, catalog, 1, -timedelta(hours=1))  # month plan: channels A+B
    async with sm() as s:
        await grant(s, 1, catalog.life, None, NOW)  # lifetime covers channel A
        await s.commit()
    await jobs.job_expirations(bot, sm, NOW)
    banned = [kw["chat_id"] for n, kw in bot.calls if n == "ban_chat_member"]
    assert banned == [-1002]


async def test_admins_are_never_removed(sm, bot, catalog):
    await make_user(sm, 999)
    await sub_ending_in(sm, catalog, 999, -timedelta(hours=1))
    await jobs.job_expirations(bot, sm, NOW)
    assert bot.count("ban_chat_member") == 0


async def test_expired_order_notification_window(sm, bot, catalog):
    await make_user(sm, 1)
    async with sm() as s:
        user = await s.get(User, 1)
        await order_svc.create_order(s, user, catalog.month, catalog.method, NOW - timedelta(days=5))
        await order_svc.create_order(s, user, catalog.month, catalog.method, NOW - timedelta(hours=25))
        await s.commit()
    assert await jobs.job_expire_orders(bot, sm, NOW) == 2
    assert len(bot.sent_to(1)) == 1  # the 5-day-old one is expired silently


async def test_review_reminder_is_rate_limited(sm, bot, catalog):
    await make_user(sm, 1)
    from tests.test_orders import new_pending_order

    order = await new_pending_order(sm, catalog)
    async with sm() as s:
        o = await s.get(Order, order.id)
        o.proof_submitted_at = NOW - timedelta(hours=4)
        await s.commit()
    assert await jobs.job_review_reminder(bot, sm, NOW) == 1
    assert await jobs.job_review_reminder(bot, sm, NOW + timedelta(hours=1)) == 0
    assert await jobs.job_review_reminder(bot, sm, NOW + timedelta(hours=4)) == 1
    assert len(bot.sent_to(999)) == 2


async def test_access_retry_after_failure(sm, bot, catalog):
    from bot.services.access import grant_order_access
    from tests.test_orders import new_pending_order

    await make_user(sm, 1)
    order = await new_pending_order(sm, catalog)
    async with sm() as s:
        await order_svc.approve_order(s, order.id, 999, NOW)
    bot.fail_invite = True
    async with sm() as s:
        assert await grant_order_access(bot, s, order.id) is False
        o = await s.get(Order, order.id)
        assert o.status == OrderStatus.APPROVED and not o.access_granted and o.access_attempts == 1
    assert any("Не удалось выдать доступ" in t for t in bot.sent_to(999))  # admin alert
    bot.fail_invite = False
    async with sm() as s:
        o = await s.get(Order, order.id)
        o.updated_at = NOW - timedelta(minutes=10)
        await s.commit()
    assert await jobs.job_access_retry(bot, sm, NOW) == 1
    async with sm() as s:
        assert (await s.get(Order, order.id)).access_granted


async def test_catch_up_recovers_pending_join_requests(sm, bot, catalog):
    await make_user(sm, 1)
    async with sm() as s:
        await grant(s, 1, catalog.life, None, NOW)
        s.add(InviteLink(user_id=1, channel_id=catalog.ch_a.id, link="https://t.me/+x",
                         expires_at=NOW + timedelta(hours=10), created_at=NOW - timedelta(hours=30)))
        await kv_set(s, "heartbeat", (NOW - timedelta(hours=30)).isoformat())
        await s.commit()
    bot.pending_requests.add((-1001, 1))
    counts = await catch_up(bot, sm)
    assert counts["joins_approved"] == 1
    assert counts["downtime_min"] >= 30 * 60 - 1


def test_daily_due():
    tz_hour = 10
    # 2026-03-01 12:00 Moscow = 09:00 UTC
    now = datetime(2026, 3, 1, 9, 0)
    assert jobs.daily_due(None, tz_hour, now)
    assert not jobs.daily_due("2026-03-01", tz_hour, now)
    assert jobs.daily_due("2026-02-28", tz_hour, now)
    early = datetime(2026, 3, 1, 4, 0)  # 07:00 Moscow, before the hour
    assert not jobs.daily_due("2026-02-28", tz_hour, early)
    assert jobs.daily_due("2026-02-25", tz_hour, early)  # missed for days -> run now
    assert settings.timezone == "Europe/Moscow"
