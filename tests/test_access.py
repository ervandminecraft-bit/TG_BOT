from types import SimpleNamespace

from sqlalchemy import select

from bot.db.models import ChannelMember, InviteLink, User
from bot.services import access as access_svc
from bot.services.subscriptions import grant
from tests.conftest import NOW, make_user


class FakeJoinRequest:
    def __init__(self, chat_id: int, user_id: int, link: str | None = None) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id)
        self.user_chat_id = user_id
        self.invite_link = SimpleNamespace(invite_link=link) if link else None
        self.result: str | None = None

    async def approve(self):
        self.result = "approved"

    async def decline(self):
        self.result = "declined"


async def test_join_request_approved_for_subscriber(sm, bot, catalog):
    await make_user(sm, 1)
    async with sm() as s:
        await grant(s, 1, catalog.month, 30, NOW)
        s.add(InviteLink(user_id=1, channel_id=catalog.ch_a.id, link="L1", expires_at=NOW))
        await s.commit()
        req = FakeJoinRequest(-1001, 1, "L1")
        assert await access_svc.handle_join_request(bot, s, req) == "approved"
        assert req.result == "approved"
        link = await s.scalar(select(InviteLink).where(InviteLink.link == "L1"))
        assert link.used_at is not None
        member = await s.scalar(select(ChannelMember))
        assert member.status == "member"


async def test_join_request_declined_for_stranger_with_forwarded_link(sm, bot, catalog):
    """A link forwarded to another person does not work: that person has no subscription."""
    await make_user(sm, 1)
    await make_user(sm, 2)
    async with sm() as s:
        await grant(s, 1, catalog.month, 30, NOW)
        await s.commit()
        req = FakeJoinRequest(-1001, 2, "L1")
        assert await access_svc.handle_join_request(bot, s, req) == "declined"
        assert req.result == "declined"
        assert len(bot.sent_to(2)) == 1


async def test_join_request_declined_for_wrong_channel(sm, bot, catalog):
    await make_user(sm, 1)
    async with sm() as s:
        await grant(s, 1, catalog.life, None, NOW)  # channel A only
        await s.commit()
        assert await access_svc.handle_join_request(bot, s, FakeJoinRequest(-1002, 1)) == "declined"


async def test_join_request_unknown_channel_ignored(sm, bot, catalog):
    async with sm() as s:
        req = FakeJoinRequest(-1009999, 5)
        assert await access_svc.handle_join_request(bot, s, req) == "ignored"
        assert req.result is None


async def test_admin_join_always_approved(sm, bot, catalog):
    async with sm() as s:
        assert await access_svc.handle_join_request(bot, s, FakeJoinRequest(-1001, 999)) == "approved"


async def test_deliver_access_one_link_per_channel(sm, bot, catalog):
    await make_user(sm, 1)
    async with sm() as s:
        await grant(s, 1, catalog.month, 30, NOW)
        await s.commit()
        user = await s.get(User, 1)
        result = await access_svc.deliver_access(bot, s, user)
        await s.commit()
    assert len(result.links) == 2 and result.ok and result.message_sent
    invites = [kw for n, kw in bot.calls if n == "create_chat_invite_link"]
    assert all(kw["creates_join_request"] is True and "member_limit" not in kw for kw in invites)
    assert all(kw["expire_date"] > 0 for kw in invites)


async def test_deliver_access_skips_members_and_approves_pending(sm, bot, catalog):
    await make_user(sm, 1)
    bot.statuses[(-1001, 1)] = "member"
    bot.pending_requests.add((-1002, 1))
    async with sm() as s:
        await grant(s, 1, catalog.month, 30, NOW)
        await s.commit()
        result = await access_svc.deliver_access(bot, s, await s.get(User, 1))
    assert [c.chat_id for c in result.already] == [-1001]
    assert [c.chat_id for c in result.approved_pending] == [-1002]
    assert bot.count("create_chat_invite_link") == 0
