"""Shared helpers: key-value state, audit log, safe sending, admin notifications."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import KV, AuditLog, User

log = logging.getLogger(__name__)


async def kv_get(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = await session.get(KV, key)
    return row.value if row else default


async def kv_set(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(KV, key)
    if row is None:
        session.add(KV(key=key, value=value))
    else:
        row.value = value


async def audit(session: AsyncSession, admin_id: int | None, action: str, target: str | None = None,
                details: str | None = None) -> None:
    session.add(AuditLog(admin_id=admin_id, action=action, target=target, details=details))


async def mark_blocked(session: AsyncSession, user_id: int) -> None:
    await session.execute(update(User).where(User.id == user_id).values(is_blocked=True))


async def safe_send(bot: Bot, session: AsyncSession | None, user_id: int, text: str, **kwargs: Any) -> Message | None:
    """Send a message to a user; returns None if the user blocked the bot or the chat is unavailable."""
    try:
        return await bot.send_message(user_id, text, **kwargs)
    except TelegramForbiddenError:
        log.info("User %s blocked the bot", user_id)
        if session is not None:
            await mark_blocked(session, user_id)
    except TelegramBadRequest as exc:
        log.warning("Cannot message user %s: %s", user_id, exc.message)
    return None


async def notify_admins(bot: Bot, text: str, **kwargs: Any) -> list[Message]:
    sent: list[Message] = []
    for admin_id in settings.admin_ids:
        try:
            sent.append(await bot.send_message(admin_id, text, **kwargs))
        except Exception as exc:  # noqa: BLE001 - admin notifications must never crash the caller
            log.warning("Cannot notify admin %s: %s", admin_id, exc)
    return sent


async def admin_languages(session: AsyncSession | None) -> dict[int, str]:
    langs = {admin_id: settings.default_language for admin_id in settings.admin_ids}
    if session is not None and langs:
        rows = await session.execute(select(User.id, User.language).where(User.id.in_(list(langs))))
        for uid, lang in rows:
            langs[uid] = lang or settings.default_language
    return langs


async def notify_admins_t(bot: Bot, session: AsyncSession | None, key: str,
                          markup: Callable[[str], Any] | None = None, **fmt: Any) -> list[Message]:
    """Send a localized message to every admin in their own language."""
    from bot.i18n import t

    sent: list[Message] = []
    for admin_id, lang in (await admin_languages(session)).items():
        try:
            sent.append(await bot.send_message(admin_id, t(lang, key, **fmt),
                                               reply_markup=markup(lang) if markup else None))
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot notify admin %s: %s", admin_id, exc)
    return sent


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def is_protected(user_id: int) -> bool:
    """Users that must never be removed from channels automatically."""
    return user_id in settings.admin_ids or user_id in settings.whitelist_ids
