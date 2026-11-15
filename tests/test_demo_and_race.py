import asyncio

from sqlalchemy import func, select

from bot.db.models import ChannelMember, PaymentMethodInfo, Plan, PlanPrice
from bot.services.access import set_member_status
from scripts import seed_demo


async def test_concurrent_member_status_does_not_fail(sm, catalog):
    """A join request and a chat_member update for the same user arrive at once (seen in production)."""
    async def write(status):
        async with sm() as s:
            await set_member_status(s, 42, catalog.ch_a.id, status)
            await s.commit()

    await asyncio.gather(write("member"), write("member"), write("left"))
    async with sm() as s:
        assert await s.scalar(select(func.count()).select_from(ChannelMember)) == 1
        await set_member_status(s, 42, catalog.ch_a.id, "kicked")
        await s.commit()
        assert (await s.scalar(select(ChannelMember.status))) == "kicked"


async def test_seed_demo_is_idempotent_and_removable(sm, catalog):
    async with sm() as s:
        methods_before = await s.scalar(select(func.count()).select_from(PaymentMethodInfo))
        plans_before = await s.scalar(select(func.count()).select_from(Plan))
    for _ in range(2):
        async with sm() as s:
            await seed_demo.add(s)
    async with sm() as s:
        assert await s.scalar(select(func.count()).select_from(PaymentMethodInfo)) == methods_before + 4
        assert await s.scalar(select(func.count()).select_from(Plan)) == plans_before + 5
        week = await s.scalar(select(Plan).where(Plan.name == seed_demo.PLANS[0][0]))
        assert week.channels  # visible in the catalog
        amounts = sorted((await s.scalars(select(PlanPrice.amount).where(PlanPrice.plan_id == week.id))).all())
        assert amounts == [250, 250, 100000, 100000]
    async with sm() as s:
        await seed_demo.remove(s)
    async with sm() as s:
        assert await s.scalar(select(func.count()).select_from(PaymentMethodInfo)) == methods_before
        assert await s.scalar(select(func.count()).select_from(Plan)) == plans_before
