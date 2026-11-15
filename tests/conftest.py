"""Test fixtures: a temporary SQLite DB and a fake Telegram bot that records calls."""
from __future__ import annotations

import os

# Must be set before bot.config is imported (env vars override .env).
os.environ.update({
    "BOT_TOKEN": "123456:TEST",
    "ADMIN_IDS": "999",
    "WHITELIST_IDS": "",
    "GRACE_DAYS": "0",
    "REF_BONUS_DAYS": "7",
    "REF_DISCOUNT_PERCENT": "10",
    "REF_MAX_REWARDS_PER_MONTH": "20",
    "ORDER_TTL_HOURS": "24",
    "TIMEZONE": "Europe/Moscow",
    "AUTO_REMOVE_UNPAID": "false",
})

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from aiogram.exceptions import TelegramBadRequest  # noqa: E402
from aiogram.methods import ApproveChatJoinRequest  # noqa: E402

from bot.db.migrations import run_migrations  # noqa: E402
from bot.db.models import Channel, PaymentMethodInfo, Plan, PlanPrice, User  # noqa: E402
from bot.db.session import make_engine, make_session_maker  # noqa: E402

from bot.utils.timeutil import utcnow  # noqa: E402

# Services that call utcnow() internally (access checks) must agree with test data.
NOW = utcnow()


class FakeBot:
    """Records every Telegram call; configurable member statuses and pending join requests."""

    def __init__(self) -> None:
        self.id = 42
        self.calls: list[tuple[str, dict]] = []
        self.statuses: dict[tuple[int, int], str] = {}
        self.pending_requests: set[tuple[int, int]] = set()
        self.fail_invite = False
        self.fail_ban = False
        self._msg_id = 0

    def _rec(self, _method: str, **kw) -> None:
        self.calls.append((_method, kw))

    def sent_to(self, chat_id: int) -> list[str]:
        return [kw["text"] for n, kw in self.calls if n == "send_message" and kw["chat_id"] == chat_id]

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    async def send_message(self, chat_id, text, **kw):
        self._rec("send_message", chat_id=chat_id, text=text, **kw)
        self._msg_id += 1
        return SimpleNamespace(message_id=self._msg_id, chat=SimpleNamespace(id=chat_id))

    async def send_photo(self, chat_id, photo, **kw):
        self._rec("send_photo", chat_id=chat_id)
        self._msg_id += 1
        return SimpleNamespace(message_id=self._msg_id)

    async def send_document(self, chat_id, document, **kw):
        self._rec("send_document", chat_id=chat_id)
        self._msg_id += 1
        return SimpleNamespace(message_id=self._msg_id)

    async def edit_message_text(self, **kw):
        self._rec("edit_message_text", **kw)

    async def get_chat_member(self, chat_id, user_id):
        self._rec("get_chat_member", chat_id=chat_id, user_id=user_id)
        return SimpleNamespace(status=self.statuses.get((chat_id, user_id), "left"))

    async def get_chat_member_count(self, chat_id):
        return 100

    async def approve_chat_join_request(self, chat_id, user_id):
        self._rec("approve_chat_join_request", chat_id=chat_id, user_id=user_id)
        if (chat_id, user_id) in self.pending_requests:
            self.pending_requests.discard((chat_id, user_id))
            self.statuses[(chat_id, user_id)] = "member"
            return True
        raise TelegramBadRequest(method=ApproveChatJoinRequest(chat_id=chat_id, user_id=user_id),
                                 message="Bad Request: HIDE_REQUESTER_MISSING")

    async def create_chat_invite_link(self, chat_id, **kw):
        self._rec("create_chat_invite_link", chat_id=chat_id, **kw)
        if self.fail_invite:
            raise TelegramBadRequest(method=ApproveChatJoinRequest(chat_id=chat_id, user_id=0),
                                     message="Bad Request: not enough rights")
        self._msg_id += 1
        return SimpleNamespace(invite_link=f"https://t.me/+link{self._msg_id}")

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self._rec("ban_chat_member", chat_id=chat_id, user_id=user_id)
        if self.fail_ban:
            raise TelegramBadRequest(method=ApproveChatJoinRequest(chat_id=chat_id, user_id=user_id),
                                     message="Bad Request: not enough rights to restrict/unrestrict chat member")
        self.statuses[(chat_id, user_id)] = "kicked"

    async def unban_chat_member(self, chat_id, user_id, **kw):
        self._rec("unban_chat_member", chat_id=chat_id, user_id=user_id, **kw)
        self.statuses[(chat_id, user_id)] = "left"


@pytest.fixture
async def sm(tmp_path):
    engine = make_engine(tmp_path / "test.sqlite3")
    await run_migrations(engine)
    maker = make_session_maker(engine)
    yield maker
    await engine.dispose()


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
async def catalog(sm):
    """Two channels, a 30-day plan for both, a lifetime plan for channel A, one RUB method."""
    async with sm() as s:
        ch_a = Channel(chat_id=-1001, title="Channel A")
        ch_b = Channel(chat_id=-1002, title="Channel B")
        method = PaymentMethodInfo(name="Card", currency="RUB", details="0000 0000")
        s.add_all([ch_a, ch_b, method])
        await s.flush()
        month = Plan(name="Month", duration_days=30, channels=[ch_a, ch_b])
        life = Plan(name="Life", duration_days=None, channels=[ch_a])
        s.add_all([month, life])
        await s.flush()
        s.add_all([PlanPrice(plan_id=month.id, payment_method_id=method.id, amount=100000),
                   PlanPrice(plan_id=life.id, payment_method_id=method.id, amount=500000)])
        await s.commit()
        return SimpleNamespace(ch_a=ch_a, ch_b=ch_b, method=method, month=month, life=life)


async def make_user(sm, user_id: int, **kw) -> User:
    async with sm() as s:
        user = User(id=user_id, language="ru", ref_code=f"R{user_id}", created_at=NOW, **kw)
        s.add(user)
        await s.commit()
        return user
