from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from bot.config import settings


class IsAdmin(BaseFilter):
    """Checked on EVERY admin handler, not only when showing buttons."""

    async def __call__(self, event: TelegramObject, event_from_user=None) -> bool:
        return event_from_user is not None and event_from_user.id in settings.admin_ids
