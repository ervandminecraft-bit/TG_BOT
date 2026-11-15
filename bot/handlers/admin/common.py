"""Admin panel plumbing: one FSM state for all text inputs, dispatched by "kind"."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware, Bot, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import User
from bot.filters import IsAdmin
from bot.i18n import t
from bot.keyboards import admin_back_kb


class AdminInput(StatesGroup):
    value = State()


@dataclass
class Ctx:
    message: Message
    state: FSMContext
    session: AsyncSession
    user: User
    bot: Bot
    session_maker: async_sessionmaker[AsyncSession]
    data: dict[str, Any]

    @property
    def lang(self) -> str:
        return self.user.language

    @property
    def text(self) -> str:
        return (self.message.text or "").strip()


InputHandler = Callable[[Ctx], Awaitable[None]]
INPUT_HANDLERS: dict[str, InputHandler] = {}


def on_input(kind: str) -> Callable[[InputHandler], InputHandler]:
    def deco(fn: InputHandler) -> InputHandler:
        INPUT_HANDLERS[kind] = fn
        return fn
    return deco


class LeaveInputOnNavigation(BaseMiddleware):
    """Pressing any admin button cancels a pending text input (wizard data is kept)."""

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        state: FSMContext | None = data.get("state")
        if state is not None and await state.get_state() == AdminInput.value.state:
            await state.set_state(None)
        return await handler(event, data)


def admin_router(name: str) -> Router:
    router = Router(name=name)
    router.message.filter(IsAdmin())
    router.callback_query.filter(IsAdmin())
    router.callback_query.middleware(LeaveInputOnNavigation())
    return router


async def ask(target: CallbackQuery | Message, state: FSMContext, lang: str, kind: str, prompt_key: str,
              back: tuple[str, str, int] = ("home", "", 0), **data: Any) -> None:
    """Switch to text-input mode and show a prompt with a Cancel/Back button."""
    await state.set_state(AdminInput.value)
    await state.update_data(kind=kind, back=list(back), **data)
    text = t(lang, prompt_key, **{k: v for k, v in data.items() if isinstance(v, (str, int))}) + "\n\n" + t(lang, "input_cancel_hint")
    kb = admin_back_kb(lang, *back)
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb)


async def edit(call: CallbackQuery, text: str, markup=None) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001
        await call.message.answer(text, reply_markup=markup)
    try:
        await call.answer()
    except Exception:  # noqa: BLE001
        pass


input_router = admin_router("admin_input")


@input_router.message(StateFilter(AdminInput.value))
async def dispatch_input(message: Message, state: FSMContext, session: AsyncSession, user: User, bot: Bot,
                         session_maker: async_sessionmaker[AsyncSession]) -> None:
    data = await state.get_data()
    kind = data.get("kind", "")
    if message.text and message.text.strip() in ("/cancel", "/menu", "/admin", "/start"):
        await state.clear()
        await message.answer(t(user.language, "action_cancelled"))
        return
    handler = INPUT_HANDLERS.get(kind)
    if handler is None:
        await state.clear()
        return
    await handler(Ctx(message, state, session, user, bot, session_maker, data))


async def answer_pair(message: Message, pair: tuple[str, Any]) -> None:
    text, markup = pair
    await message.answer(text, reply_markup=markup)


async def done(ctx: Ctx, text: str, markup=None) -> None:
    await ctx.state.clear()
    await ctx.message.answer(text, reply_markup=markup)


async def retry(ctx: Ctx, key: str, **fmt: Any) -> None:
    """Invalid input: stay in the same state and explain."""
    back = ctx.data.get("back") or ["home", "", 0]
    await ctx.message.answer(t(ctx.lang, key, **fmt) + "\n\n" + t(ctx.lang, "input_cancel_hint"),
                             reply_markup=admin_back_kb(ctx.lang, *back))
