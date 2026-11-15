"""Channel access: personal invite links (join requests), approval, removal."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatJoinRequest, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Channel, ChannelMember, InviteLink, Order, OrderStatus, User
from bot.i18n import t
from bot.keyboards import MenuCB
from bot.services.common import is_protected, notify_admins_t, safe_send
from bot.services.subscriptions import active_channel_ids, has_access
from bot.utils.text import esc
from bot.utils.timeutil import unix, utcnow

log = logging.getLogger(__name__)

MEMBER_STATUSES = {"member", "administrator", "creator"}
MAX_ACCESS_ATTEMPTS = 10


async def get_member_status(bot: Bot, chat_id: int, user_id: int) -> str | None:
    """Return 'member' / 'left' / 'kicked' / ... or None if Telegram did not answer."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError as exc:
        log.warning("get_chat_member(%s, %s) failed: %s", chat_id, user_id, exc)
        return None
    status = member.status.value if hasattr(member.status, "value") else str(member.status)
    if status == "restricted":
        return "member" if getattr(member, "is_member", False) else "left"
    return status


async def set_member_status(session: AsyncSession, user_id: int, channel_id: int, status: str) -> None:
    """Atomic upsert: a join request and a chat_member update for the same user often arrive
    at the same moment and are processed concurrently, so select-then-insert would race."""
    now = utcnow()
    stmt = sqlite_insert(ChannelMember).values(user_id=user_id, channel_id=channel_id, status=status, updated_at=now)
    stmt = stmt.on_conflict_do_update(index_elements=["user_id", "channel_id"],
                                      set_={"status": status, "updated_at": now})
    await session.execute(stmt)


async def try_approve_pending(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Approve a join request that may be pending since the bot was offline."""
    try:
        return bool(await bot.approve_chat_join_request(chat_id, user_id))
    except TelegramBadRequest:
        return False  # no pending request (HIDE_REQUESTER_MISSING) or already a member


async def create_invite_link(bot: Bot, session: AsyncSession, user_id: int, channel: Channel,
                             now: datetime) -> str:
    """Personal link with a join request; only a user with an active subscription will be approved."""
    expire = now + timedelta(hours=settings.invite_link_ttl_hours)
    invite = await bot.create_chat_invite_link(
        chat_id=channel.chat_id,
        name=f"sub {user_id}"[:32],
        expire_date=unix(expire),
        creates_join_request=True,
    )
    session.add(InviteLink(user_id=user_id, channel_id=channel.id, link=invite.invite_link,
                           expires_at=expire, created_at=now))
    return invite.invite_link


@dataclass
class DeliveryResult:
    links: list[tuple[Channel, str]] = field(default_factory=list)
    already: list[Channel] = field(default_factory=list)
    approved_pending: list[Channel] = field(default_factory=list)
    failed: list[tuple[Channel, str]] = field(default_factory=list)
    message_sent: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed


async def deliver_access(bot: Bot, session: AsyncSession, user: User, now: datetime | None = None,
                         header_key: str = "access_granted", channel_ids: set[int] | None = None) -> DeliveryResult:
    """Send the user one button per channel they have access to."""
    now = now or utcnow()
    allowed = await active_channel_ids(session, user.id, now)
    if channel_ids is not None:
        allowed &= channel_ids
    result = DeliveryResult()
    if not allowed:
        return result
    channels = (await session.scalars(select(Channel).where(Channel.id.in_(allowed)).order_by(Channel.id))).all()
    for channel in channels:
        status = await get_member_status(bot, channel.chat_id, user.id)
        if status in MEMBER_STATUSES:
            result.already.append(channel)
            await set_member_status(session, user.id, channel.id, "member")
            continue
        if await try_approve_pending(bot, channel.chat_id, user.id):
            result.approved_pending.append(channel)
            await set_member_status(session, user.id, channel.id, "member")
            continue
        try:
            link = await create_invite_link(bot, session, user.id, channel, now)
            result.links.append((channel, link))
        except TelegramAPIError as exc:
            log.error("Cannot create invite link for channel %s: %s", channel.chat_id, exc)
            result.failed.append((channel, str(exc)))

    lang = user.language
    lines = [t(lang, header_key)]
    for channel in result.already + result.approved_pending:
        lines.append(t(lang, "access_already_member", channel=esc(channel.title)))
    if result.links:
        lines.append(t(lang, "access_links_hint", hours=settings.invite_link_ttl_hours))
    if result.failed:
        lines.append(t(lang, "access_partial_failure"))
    rows = [[InlineKeyboardButton(text=t(lang, "btn_join_channel", channel=channel.title[:40]), url=link)]
            for channel, link in result.links]
    rows.append([InlineKeyboardButton(text=t(lang, "btn_main_menu"), callback_data=MenuCB(action="main").pack())])
    if result.links or result.already or result.approved_pending or result.failed:
        msg = await safe_send(bot, session, user.id, "\n\n".join(lines),
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        result.message_sent = msg is not None
    return result


async def grant_order_access(bot: Bot, session: AsyncSession, order_id: int) -> bool:
    """Deliver access for an approved order. Safe to call repeatedly (scheduler retries)."""
    order = await session.get(Order, order_id)
    if order is None or order.status != OrderStatus.APPROVED or order.access_granted:
        return True
    user = await session.get(User, order.user_id)
    try:
        result = await deliver_access(bot, session, user)
        error = "; ".join(f"{c.title}: {e}" for c, e in result.failed) if result.failed else None
        if result.ok and not result.message_sent and not user.is_blocked:
            error = "message not delivered"
    except Exception as exc:  # noqa: BLE001
        log.exception("Access delivery failed for order %s", order.code)
        error = str(exc)
    if error is None or user.is_blocked:
        order.access_granted = True
        order.access_last_error = error
        await session.commit()
        if user.is_blocked:
            await notify_admins_t(bot, session, "admin_alert_user_blocked", code=order.code, user_id=user.id)
        return True
    order.access_attempts += 1
    order.access_last_error = error[:1000]
    await session.commit()
    if order.access_attempts in (1, MAX_ACCESS_ATTEMPTS):
        await notify_admins_t(bot, session, "admin_alert_access_failed", code=order.code, user_id=user.id,
                              attempts=order.access_attempts, error=esc(error[:500]))
    return False


async def remove_from_channel(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Kick the user (ban + unban), so they can come back after paying again."""
    if is_protected(user_id):
        return False
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        return True
    except TelegramAPIError as exc:
        log.warning("Cannot remove %s from %s: %s", user_id, chat_id, exc)
        return False


async def remove_uncovered(bot: Bot, session: AsyncSession, user_id: int, channel_ids: set[int],
                           now: datetime) -> tuple[list[Channel], list[Channel]]:
    """Remove the user from the given channels unless another active subscription still covers them.

    Returns (removed, failed). A failure usually means the bot lacks the "ban users" right.
    """
    removed: list[Channel] = []
    failed: list[Channel] = []
    if is_protected(user_id):
        return removed, failed
    still_allowed = await active_channel_ids(session, user_id, now)
    to_remove = channel_ids - still_allowed
    if not to_remove:
        return removed, failed
    channels = (await session.scalars(select(Channel).where(Channel.id.in_(to_remove)))).all()
    for channel in channels:
        if await remove_from_channel(bot, channel.chat_id, user_id):
            removed.append(channel)
            await set_member_status(session, user_id, channel.id, "left")
        else:
            failed.append(channel)
    return removed, failed


async def handle_join_request(bot: Bot, session: AsyncSession, request: ChatJoinRequest) -> str:
    """Approve only users with an active subscription for this channel. Returns the decision."""
    channel = await session.scalar(select(Channel).where(Channel.chat_id == request.chat.id))
    if channel is None:
        return "ignored"  # not a channel managed by this bot
    user_id = request.from_user.id
    now = utcnow()
    allowed = is_protected(user_id) or (channel.is_active and await has_access(session, user_id, channel.id, now))
    if allowed:
        try:
            await request.approve()
        except TelegramBadRequest as exc:
            log.info("Join request approve failed (%s): %s", user_id, exc.message)
            return "error"
        await set_member_status(session, user_id, channel.id, "member")
        if request.invite_link:
            link = await session.scalar(select(InviteLink).where(InviteLink.link == request.invite_link.invite_link))
            if link is not None:
                link.used_at = now
        await session.commit()
        return "approved"
    try:
        await request.decline()
    except TelegramBadRequest as exc:
        log.info("Join request decline failed (%s): %s", user_id, exc.message)
    user = await session.get(User, user_id)
    lang = user.language if user else "ru"
    try:
        await bot.send_message(
            request.user_chat_id, t(lang, "join_declined", channel=esc(channel.title)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=t(lang, "btn_catalog"), callback_data=MenuCB(action="catalog").pack())
            ]]),
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    return "declined"


@dataclass
class RightsCheck:
    error: str | None  # i18n key: the channel cannot be used
    warning: str | None  # i18n key: usable, but something may not work
    title: str | None
    chat_id: int | None


async def check_bot_rights(bot: Bot, chat_id: int | str) -> RightsCheck:
    """The bot must be an admin with can_invite_users (links + join requests).
    can_restrict_members (removing expired users) is a warning: verify it on a real channel."""
    try:
        chat = await bot.get_chat(chat_id)
    except TelegramAPIError:
        return RightsCheck("ch_err_not_found", None, None, None)
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramAPIError:
        return RightsCheck("ch_err_not_admin", None, chat.title, chat.id)
    status = member.status.value if hasattr(member.status, "value") else str(member.status)
    if status != "administrator":
        return RightsCheck("ch_err_not_admin", None, chat.title, chat.id)
    if not getattr(member, "can_invite_users", False):
        return RightsCheck("ch_err_no_invite", None, chat.title, chat.id)
    warning = None if getattr(member, "can_restrict_members", False) else "ch_err_no_restrict"
    return RightsCheck(None, warning, chat.title, chat.id)
