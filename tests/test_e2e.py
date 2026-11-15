"""End-to-end: real aiogram Dispatcher + handlers, Telegram API mocked at the HTTP-session level."""
from __future__ import annotations

import itertools
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatInviteLink,
    ChatJoinRequest,
    Message,
    MessageId,
    PhotoSize,
    Update,
)
from aiogram.types import User as TgUser
from sqlalchemy import select

from bot.config import settings
from bot.db.models import Channel, Order, OrderStatus, PaymentMethodInfo, Plan, PromoCode, Subscription
from bot.main import build_dispatcher

ADMIN, BUYER, BOT_ID = 999, 555, 42
CHANNEL = -1001234567890


class MockSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list = []
        self.pending_joins: set[tuple[int, int]] = set()
        self.ids = itertools.count(1000)

    async def close(self) -> None:
        return None

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    def texts_to(self, chat_id: int) -> list[str]:
        out = []
        for m in self.requests:
            name = type(m).__name__
            if name in ("SendMessage", "EditMessageText") and getattr(m, "chat_id", None) == chat_id:
                out.append(m.text)
        return out

    def of(self, name: str) -> list:
        return [m for m in self.requests if type(m).__name__ == name]

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        name = type(method).__name__
        now = datetime.now(timezone.utc)
        if name == "GetMe":
            return TgUser(id=BOT_ID, is_bot=True, first_name="Bot", username="test_bot")
        if name in ("SendMessage", "SendPhoto", "SendDocument"):
            return Message(message_id=next(self.ids), date=now, chat=Chat(id=method.chat_id, type="private"),
                           text=getattr(method, "text", None))
        if name == "CopyMessage":
            return MessageId(message_id=next(self.ids))
        if name == "GetChat":
            return SimpleNamespace(id=CHANNEL, title="Secret Channel")
        if name == "GetChatMember":
            if method.user_id == BOT_ID:
                return SimpleNamespace(status="administrator", can_invite_users=True, can_restrict_members=True)
            return SimpleNamespace(status="left")
        if name == "CreateChatInviteLink":
            return ChatInviteLink(invite_link=f"https://t.me/+inv{next(self.ids)}", creator=TgUser(
                id=BOT_ID, is_bot=True, first_name="Bot"), creates_join_request=True, is_primary=False,
                is_revoked=False)
        if name == "ApproveChatJoinRequest":
            key = (method.chat_id, method.user_id)
            if key in self.pending_joins:
                self.pending_joins.discard(key)
                return True
            raise TelegramBadRequest(method=method, message="Bad Request: HIDE_REQUESTER_MISSING")
        if name == "GetChatMemberCount":
            return 3
        return True  # answerCallbackQuery, edit*, ban/unban, decline, setMyCommands...


class World:
    def __init__(self, dp, bot: Bot, session: MockSession) -> None:
        self.dp, self.bot, self.api = dp, bot, session
        self.uid = itertools.count(1)

    def _user(self, uid: int) -> TgUser:
        return TgUser(id=uid, is_bot=False, first_name=f"U{uid}", username=f"user{uid}", language_code="ru")

    def _msg(self, uid: int, **kw) -> Message:
        return Message(message_id=next(self.uid), date=datetime.now(timezone.utc),
                       chat=Chat(id=uid, type="private"), from_user=self._user(uid), **kw)

    async def text(self, uid: int, text: str) -> None:
        await self.dp.feed_update(self.bot, Update(update_id=next(self.uid), message=self._msg(uid, text=text)))

    async def photo(self, uid: int, unique: str) -> None:
        photo = [PhotoSize(file_id="F" + unique, file_unique_id=unique, width=100, height=100, file_size=1000)]
        await self.dp.feed_update(self.bot, Update(update_id=next(self.uid), message=self._msg(uid, photo=photo)))

    async def click(self, uid: int, data) -> None:
        packed = data if isinstance(data, str) else data.pack()
        cq = CallbackQuery(id=str(next(self.uid)), from_user=self._user(uid), chat_instance="x", data=packed,
                           message=self._msg(uid, text="old"))
        await self.dp.feed_update(self.bot, Update(update_id=next(self.uid), callback_query=cq))

    async def join_request(self, uid: int, link: str | None) -> None:
        req = ChatJoinRequest(chat=Chat(id=CHANNEL, type="channel", title="Secret Channel"),
                              from_user=self._user(uid), user_chat_id=uid, date=datetime.now(timezone.utc),
                              invite_link=ChatInviteLink(invite_link=link, creator=self._user(BOT_ID),
                                                         creates_join_request=True, is_primary=False,
                                                         is_revoked=False) if link else None)
        await self.dp.feed_update(self.bot, Update(update_id=next(self.uid), chat_join_request=req))


@pytest.fixture
async def world(sm, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.sqlite3"))
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    api = MockSession()
    bot = Bot("42:TEST", session=api, default=DefaultBotProperties(parse_mode="HTML"))
    # Routers are module-level singletons: detach them from the previous test's dispatcher.
    from bot.handlers import access, orders, review, user
    from bot.handlers.admin import broadcast, catalog, common, home, promo, users

    for mod in (access, orders, review, user, broadcast, catalog, home, promo, users):
        mod.router._parent_router = None
    common.input_router._parent_router = None
    dp = build_dispatcher(sm)
    return World(dp, bot, api)


async def test_full_purchase_flow(world: World, sm):
    from bot.keyboards import AdminCB, MenuCB, OrderCB, PlanCB, ReviewCB

    w = world
    # --- admin sets up a payment method
    await w.text(ADMIN, "/start")
    await w.click(ADMIN, AdminCB(s="home"))
    await w.click(ADMIN, AdminCB(s="pm", a="add"))
    await w.text(ADMIN, "Карта")
    await w.text(ADMIN, "rub")
    await w.text(ADMIN, "Номер карты 0000 0000 0000 0000")
    async with sm() as s:
        method = await s.scalar(select(PaymentMethodInfo))
        assert method.name == "Карта" and method.currency == "RUB"

    # --- admin adds a channel (bot rights are checked)
    await w.click(ADMIN, AdminCB(s="ch", a="add"))
    await w.text(ADMIN, str(CHANNEL))
    async with sm() as s:
        channel = await s.scalar(select(Channel))
        assert channel.chat_id == CHANNEL and channel.title == "Secret Channel"

    # --- admin creates a plan, attaches the channel and a price
    await w.click(ADMIN, AdminCB(s="pl", a="add"))
    await w.text(ADMIN, "Месяц / Month")
    await w.text(ADMIN, "Доступ к каналу")
    await w.text(ADMIN, "30")
    async with sm() as s:
        plan = await s.scalar(select(Plan))
        assert plan.duration_days == 30
    await w.click(ADMIN, AdminCB(s="pl", a="cht", i=plan.id, x=str(channel.id)))
    await w.click(ADMIN, AdminCB(s="pl", a="price", i=plan.id, x=str(method.id)))
    await w.text(ADMIN, "1490")
    async with sm() as s:
        plan = await s.get(Plan, plan.id)
        assert [c.id for c in plan.channels] == [channel.id]
        assert plan.prices[0].amount == 149000

    # --- admin creates a promo code through the wizard
    await w.click(ADMIN, AdminCB(s="promo", a="add"))
    await w.text(ADMIN, "sale10")
    await w.click(ADMIN, AdminCB(s="promo", a="kind", x="percent"))
    await w.text(ADMIN, "10")
    await w.text(ADMIN, "3")   # bonus days
    await w.text(ADMIN, "0")   # valid forever
    await w.text(ADMIN, "0")   # unlimited uses
    await w.click(ADMIN, AdminCB(s="promo", a="once"))
    await w.click(ADMIN, AdminCB(s="promo", a="save", x="1"))
    async with sm() as s:
        promo = await s.scalar(select(PromoCode))
        assert promo.code == "SALE10" and promo.value == 10 and promo.bonus_days == 3

    # --- buyer: start, promo, catalog, buy
    await w.text(BUYER, "/start")
    await w.click(BUYER, MenuCB(action="promo"))
    await w.text(BUYER, "SALE10")
    await w.click(BUYER, MenuCB(action="catalog"))
    await w.click(BUYER, PlanCB(action="view", plan_id=plan.id))
    await w.click(BUYER, PlanCB(action="buy", plan_id=plan.id, method_id=method.id))
    async with sm() as s:
        order = await s.scalar(select(Order))
        assert order.status == OrderStatus.AWAITING_PAYMENT
        assert order.final_amount == 134100 and order.bonus_days == 3
    assert any(order.code in t for t in w.api.texts_to(BUYER))

    # --- buyer pays and sends a screenshot
    await w.click(BUYER, OrderCB(action="paid", order_id=order.id))
    await w.photo(BUYER, "receipt1")
    async with sm() as s:
        order = await s.get(Order, order.id)
        assert order.status == OrderStatus.PENDING_REVIEW and order.proof_file_unique_id == "receipt1"
    assert w.api.of("SendPhoto"), "admin must receive the receipt"
    assert any("1 341 RUB" in t and order.code in t for t in w.api.texts_to(ADMIN))

    # --- admin approves; second click says "already processed"
    await w.click(ADMIN, ReviewCB(action="ok", order_id=order.id))
    await w.click(ADMIN, ReviewCB(action="ok", order_id=order.id))
    async with sm() as s:
        order = await s.get(Order, order.id)
        assert order.status == OrderStatus.APPROVED and order.access_granted
        sub = await s.scalar(select(Subscription))
        assert sub.user_id == BUYER
    links = w.api.of("CreateChatInviteLink")
    assert len(links) == 1 and links[0].creates_join_request
    assert any("Доступ открыт" in t for t in w.api.texts_to(BUYER))
    alerts = [m for m in w.api.of("AnswerCallbackQuery") if m.text and "Уже обработано" in m.text]
    assert alerts, "second approve must answer 'already processed'"

    # --- buyer joins with the personal link; a stranger with the same link is declined
    await w.join_request(BUYER, "https://t.me/+x")
    await w.join_request(777, "https://t.me/+x")
    approved = [m for m in w.api.of("ApproveChatJoinRequest") if m.user_id == BUYER]
    declined = [m for m in w.api.of("DeclineChatJoinRequest") if m.user_id == 777]
    assert approved and declined

    # --- my subscription and re-link
    await w.click(BUYER, MenuCB(action="mysub"))
    assert any("Действует до" in t for t in w.api.texts_to(BUYER))

    # --- admin tools: stats, export, audit, user card, backup, queue
    await w.click(ADMIN, AdminCB(s="stats"))
    assert any("1 341 RUB" in t for t in w.api.texts_to(ADMIN))
    await w.click(ADMIN, AdminCB(s="export", a="orders"))
    await w.click(ADMIN, AdminCB(s="export", a="subs"))
    assert len(w.api.of("SendDocument")) >= 2
    await w.click(ADMIN, AdminCB(s="audit"))
    assert any("order_approve" in t for t in w.api.texts_to(ADMIN))
    await w.click(ADMIN, AdminCB(s="orders", a="queue"))
    await w.click(ADMIN, AdminCB(s="users"))
    await w.text(ADMIN, f"@user{BUYER}")
    assert any("Подписки" in t for t in w.api.texts_to(ADMIN))
    await w.click(ADMIN, AdminCB(s="backup"))


async def test_reject_and_manual_grant_and_revoke(world: World, sm, catalog):
    from bot.keyboards import AdminCB, OrderCB, PlanCB, ReviewCB

    w = world
    await w.text(BUYER, "/start")
    await w.click(BUYER, PlanCB(action="buy", plan_id=catalog.month.id, method_id=catalog.method.id))
    async with sm() as s:
        order = await s.scalar(select(Order))
    await w.click(BUYER, OrderCB(action="paid", order_id=order.id))
    await w.text(BUYER, "Операция 1234567890")
    await w.text(ADMIN, "/start")
    await w.click(ADMIN, ReviewCB(action="no", order_id=order.id))
    await w.click(ADMIN, ReviewCB(action="custom", order_id=order.id))
    await w.text(ADMIN, "Сумма не совпадает")
    async with sm() as s:
        order = await s.get(Order, order.id)
        assert order.status == OrderStatus.REJECTED and order.reject_reason == "Сумма не совпадает"
    assert any("Сумма не совпадает" in t for t in w.api.texts_to(BUYER))

    # buyer re-sends a receipt -> back to review
    await w.click(BUYER, OrderCB(action="paid", order_id=order.id))
    await w.text(BUYER, "Операция 1234567890 повторно")
    async with sm() as s:
        assert (await s.get(Order, order.id)).status == OrderStatus.PENDING_REVIEW

    # manual grant 10 days, then revoke
    await w.click(ADMIN, AdminCB(s="users", a="grantp", i=BUYER, x=str(catalog.life.id)))
    await w.text(ADMIN, "10")
    async with sm() as s:
        sub = await s.scalar(select(Subscription).where(Subscription.plan_id == catalog.life.id))
        assert sub is not None and sub.source == "manual"
    await w.click(ADMIN, AdminCB(s="users", a="revokey", i=sub.id))
    async with sm() as s:
        assert (await s.get(Subscription, sub.id)).status == "revoked"
    assert w.api.of("BanChatMember") and w.api.of("UnbanChatMember")


async def test_non_admin_cannot_use_admin_actions(world: World, sm, catalog):
    from bot.keyboards import AdminCB, ReviewCB

    w = world
    await w.text(BUYER, "/start")
    await w.click(BUYER, AdminCB(s="pl", a="toggle", i=catalog.month.id))
    await w.click(BUYER, ReviewCB(action="ok", order_id=1))
    await w.text(BUYER, "/admin")
    async with sm() as s:
        assert (await s.get(Plan, catalog.month.id)).is_active is True
    from bot.i18n.ru import TEXTS

    assert TEXTS["adm_home"] not in w.api.texts_to(BUYER)
    assert not w.api.of("SendPhoto")  # no order card was sent anywhere


async def test_broadcast_flow(world: World, sm):
    import asyncio

    from bot.keyboards import AdminCB
    from bot.services import broadcast as bc_svc

    w = world
    for uid in (BUYER, 556, 557):
        await w.text(uid, "/start")
    await w.text(ADMIN, "/start")
    await w.click(ADMIN, AdminCB(s="bc", a="seg", x="all"))
    await w.text(ADMIN, "Новости канала!")
    await w.click(ADMIN, AdminCB(s="bc", a="go"))
    await asyncio.gather(*list(bc_svc._tasks))
    copies = w.api.of("CopyMessage")
    assert len(copies) == 1 + 4  # preview to admin + 4 users (3 buyers + admin)
    assert any("Рассылка завершена" in t for t in w.api.texts_to(ADMIN))
