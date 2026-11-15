"""Channel events: join requests (approve only paying users) and membership updates."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.types import ChatJoinRequest, ChatMemberUpdated
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Channel
from bot.services import access as access_svc

log = logging.getLogger(__name__)

router = Router(name="access")


@router.chat_join_request()
async def on_join_request(request: ChatJoinRequest, session: AsyncSession, bot: Bot) -> None:
    decision = await access_svc.handle_join_request(bot, session, request)
    log.info("Join request user=%s chat=%s -> %s", request.from_user.id, request.chat.id, decision)


@router.chat_member()
async def on_chat_member(update: ChatMemberUpdated, session: AsyncSession) -> None:
    """Track who is in the channel, so the daily reconciliation knows about them."""
    channel = await session.scalar(select(Channel).where(Channel.chat_id == update.chat.id))
    if channel is None or update.new_chat_member.user.is_bot:
        return
    member = update.new_chat_member
    status = member.status.value if hasattr(member.status, "value") else str(member.status)
    if status == "restricted":
        status = "member" if getattr(member, "is_member", False) else "left"
    await access_svc.set_member_status(session, member.user.id, channel.id, status)
    await session.commit()
