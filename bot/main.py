"""Entry point: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand, LinkPreviewOptions

from bot.config import settings
from bot.db.fsm_storage import SQLiteStorage
from bot.db.migrations import run_migrations
from bot.db.session import make_engine, make_session_maker
from bot.handlers import access, orders, review, user
from bot.handlers.admin import broadcast, catalog, common, home, promo, users
from bot.middlewares.core import AntiFloodMiddleware, DbSessionMiddleware, RetryRequestMiddleware, UserMiddleware
from bot.scheduler.runner import Scheduler, announce_start, catch_up

log = logging.getLogger("bot")


class RedactFilter(logging.Filter):
    """Never let the bot token reach the logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        token = settings.bot_token
        if token:
            msg = record.getMessage()
            if token in msg:
                record.msg = msg.replace(token, "***")
                record.args = ()
        return True


def setup_logging() -> None:
    settings.log_path.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    file = RotatingFileHandler(settings.log_path / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5,
                               encoding="utf-8")
    file.setFormatter(fmt)
    redact = RedactFilter()
    for handler in (stream, file):
        handler.addFilter(redact)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(stream)
    root.addHandler(file)
    root.setLevel(settings.log_level.upper())
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)


def check_config() -> list[str]:
    problems = []
    if not settings.bot_token or ":" not in settings.bot_token:
        problems.append("BOT_TOKEN is empty or invalid (get it from @BotFather) / BOT_TOKEN пуст или неверен")
    if not settings.admin_ids:
        problems.append("ADMIN_IDS is empty (your numeric Telegram ID) / ADMIN_IDS пуст (ваш числовой ID)")
    return problems


def build_dispatcher(session_maker) -> Dispatcher:
    dp = Dispatcher(storage=SQLiteStorage(session_maker))
    dp["session_maker"] = session_maker
    flood = AntiFloodMiddleware()
    dp.message.outer_middleware(flood)
    dp.callback_query.outer_middleware(flood)
    db = DbSessionMiddleware(session_maker)
    for observer in (dp.message, dp.callback_query, dp.chat_join_request, dp.chat_member):
        observer.outer_middleware(db)
    user_mw = UserMiddleware()
    dp.message.outer_middleware(user_mw)
    dp.callback_query.outer_middleware(user_mw)
    # Order matters: admin & review routers first, the catch-all receipt handler (orders) last.
    dp.include_routers(
        access.router,
        review.router,
        common.input_router,
        home.router,
        catalog.router,
        users.router,
        promo.router,
        broadcast.router,
        user.router,
        orders.router,
    )
    return dp


def keep_windows_awake() -> None:
    """Ask Windows not to go to sleep from inactivity while the bot runs (closing the lid still sleeps)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        es_continuous, es_system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)
    except Exception:  # noqa: BLE001
        log.warning("Could not disable Windows idle sleep")


async def run() -> None:
    setup_logging()
    keep_windows_awake()
    problems = check_config()
    if problems:
        for p in problems:
            log.error(p)
        log.error("Fix the .env file and start again / Исправьте файл .env и запустите снова")
        sys.exit(2)

    engine = make_engine(settings.db_file)
    applied = await run_migrations(engine)
    if applied:
        log.info("Database migrations applied: %s", applied)
    session_maker = make_session_maker(engine)

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)))
    bot.session.middleware(RetryRequestMiddleware())
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        log.error("Telegram rejected BOT_TOKEN. Check it in .env / Telegram не принял BOT_TOKEN, проверьте .env")
        await bot.session.close()
        sys.exit(2)
    log.info("Bot @%s started (id %s), admins: %s", me.username, me.id, len(settings.admin_ids))

    await bot.set_my_commands([
        BotCommand(command="start", description="Menu / Меню"),
        BotCommand(command="cancel", description="Cancel / Отмена"),
    ])

    dp = build_dispatcher(session_maker)
    counts = await catch_up(bot, session_maker)
    log.info("Catch-up after start: %s", counts)
    await announce_start(bot, session_maker, counts)

    scheduler = Scheduler(bot, session_maker)
    scheduler.start()
    try:
        # Updates are NOT dropped: Telegram keeps them up to 24 h, so receipts, button
        # presses and join requests sent while the laptop was off are processed now.
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await scheduler.stop()
        await bot.session.close()
        await engine.dispose()
        log.info("Bot stopped")


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code not in (0, None):
            raise


if __name__ == "__main__":
    main()
