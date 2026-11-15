"""Middlewares: DB session, user registration, anti-flood, Telegram request retries."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import settings
from bot.db.models import User
from bot.i18n import detect_language, t
from bot.services.referral import generate_ref_code
from bot.utils.timeutil import utcnow

log = logging.getLogger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self.session_maker = session_maker

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        async with self.session_maker() as session:
            data["session"] = session
            return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    """Creates/updates the user row and puts `user` and `lang` into handler data."""

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        session: AsyncSession | None = data.get("session")
        if tg_user is None or session is None or tg_user.is_bot:
            return await handler(event, data)
        # Only private chats register users.
        chat = data.get("event_chat")
        if chat is not None and chat.type != "private":
            return await handler(event, data)
        user = await session.get(User, tg_user.id)
        is_new = user is None
        now = utcnow()
        if user is None:
            user = User(
                id=tg_user.id,
                language=detect_language(tg_user.language_code, settings.default_language),
                ref_code=await generate_ref_code(session),
                created_at=now,
            )
            session.add(user)
        user.username = (tg_user.username or None)
        user.first_name = (tg_user.first_name or "")[:128] or None
        user.last_name = (tg_user.last_name or "")[:128] or None
        user.is_blocked = False
        user.last_seen_at = now
        await session.commit()
        data["user"] = user
        data["lang"] = user.language
        data["is_new_user"] = is_new
        return await handler(event, data)


class AntiFloodMiddleware(BaseMiddleware):
    """Limits messages/callbacks per user and files per minute. Admins are exempt."""

    def __init__(self) -> None:
        self.events: dict[int, deque[float]] = defaultdict(deque)
        self.files: dict[int, deque[float]] = defaultdict(deque)
        self.warned: dict[int, float] = {}

    @staticmethod
    def _hit(bucket: deque[float], now: float, window: float, limit: int) -> bool:
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.id in settings.admin_ids:
            return await handler(event, data)
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)
        now = time.monotonic()
        uid = tg_user.id
        ok = self._hit(self.events[uid], now, settings.flood_window_seconds, settings.flood_max_messages)
        if ok and isinstance(event, Message) and (event.photo or event.document or event.video or event.audio):
            ok = self._hit(self.files[uid], now, 60, settings.flood_max_files)
        if ok:
            return await handler(event, data)
        if now - self.warned.get(uid, 0) > 30:
            self.warned[uid] = now
            lang = detect_language(tg_user.language_code, settings.default_language)
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(t(lang, "flood_warning"), show_alert=False)
                else:
                    await event.answer(t(lang, "flood_warning"))
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(event, CallbackQuery):
            try:
                await event.answer()
            except Exception:  # noqa: BLE001
                pass
        return None


class RetryRequestMiddleware(BaseRequestMiddleware):
    """Retries Telegram API calls on flood limits (RetryAfter) and network/server errors with backoff."""

    def __init__(self, max_attempts: int = 4, base_delay: float = 1.0) -> None:
        self.max_attempts = max_attempts
        self.base_delay = base_delay

    async def __call__(self, make_request: NextRequestMiddlewareType[TelegramType], bot: Bot,
                       method: TelegramMethod[TelegramType]) -> Response[TelegramType]:
        attempt = 0
        while True:
            attempt += 1
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                if attempt >= self.max_attempts or exc.retry_after > 120:
                    raise
                log.warning("Flood limit on %s, sleeping %ss", type(method).__name__, exc.retry_after)
                await asyncio.sleep(exc.retry_after + 0.5)
            except (TelegramNetworkError, TelegramServerError) as exc:
                if attempt >= self.max_attempts or type(method).__name__ == "GetUpdates":
                    raise
                delay = self.base_delay * (2 ** (attempt - 1))
                log.warning("Telegram error on %s (%s), retry in %.1fs", type(method).__name__, exc, delay)
                await asyncio.sleep(delay)
