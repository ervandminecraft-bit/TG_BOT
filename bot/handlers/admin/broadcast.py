"""Admin: broadcast with segment choice, preview, confirmation and delivery report."""
from __future__ import annotations

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import Broadcast, User
from bot.handlers.admin.common import Ctx, admin_router, ask, edit, on_input
from bot.i18n import t
from bot.keyboards import AdminCB
from bot.services import broadcast as bc_svc
from bot.services.common import audit
from bot.utils.timeutil import utcnow

router = admin_router("admin_broadcast")


@router.callback_query(AdminCB.filter((F.s == "bc") & (F.a == "")))
async def cb_bc(call: CallbackQuery, user: User) -> None:
    lang = user.language
    b = InlineKeyboardBuilder()
    for seg in bc_svc.SEGMENTS:
        b.button(text=t(lang, f"bc_seg_{seg}"), callback_data=AdminCB(s="bc", a="seg", x=seg))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="home"))
    b.adjust(1)
    await edit(call, t(lang, "bc_title"), b.as_markup())


@router.callback_query(AdminCB.filter((F.s == "bc") & (F.a == "seg")))
async def cb_bc_segment(call: CallbackQuery, callback_data: AdminCB, user: User, state: FSMContext) -> None:
    segment = callback_data.x if callback_data.x in bc_svc.SEGMENTS else "all"
    await ask(call, state, user.language, "bc_message", "bc_send_message", back=("bc", "", 0), segment=segment)


@on_input("bc_message")
async def in_bc_message(ctx: Ctx) -> None:
    segment = ctx.data.get("segment", "all")
    count = len(await bc_svc.recipients(ctx.session, segment, utcnow()))
    await ctx.state.set_state(None)
    await ctx.state.update_data(bc={"segment": segment, "chat": ctx.message.chat.id, "msg": ctx.message.message_id})
    await ctx.message.answer(t(ctx.lang, "bc_preview_title"))
    await ctx.bot.copy_message(ctx.message.chat.id, ctx.message.chat.id, ctx.message.message_id)
    b = InlineKeyboardBuilder()
    b.button(text=t(ctx.lang, "bc_btn_send", n=count), callback_data=AdminCB(s="bc", a="go"))
    b.button(text=t(ctx.lang, "bc_btn_cancel"), callback_data=AdminCB(s="bc"))
    b.adjust(1)
    await ctx.message.answer(t(ctx.lang, "bc_confirm", n=count, segment=t(ctx.lang, f"bc_seg_{segment}")),
                             reply_markup=b.as_markup())


@router.callback_query(AdminCB.filter((F.s == "bc") & (F.a == "go")))
async def cb_bc_go(call: CallbackQuery, session: AsyncSession, user: User, bot: Bot, state: FSMContext,
                   session_maker: async_sessionmaker[AsyncSession]) -> None:
    data = (await state.get_data()).get("bc")
    if not data:
        await call.answer(t(user.language, "adm_wizard_lost"), show_alert=True)
        return
    await state.update_data(bc=None)
    bc = Broadcast(admin_id=user.id, segment=data["segment"], from_chat_id=data["chat"], message_id=data["msg"])
    session.add(bc)
    await audit(session, user.id, "broadcast_start", None, data["segment"])
    await session.commit()
    bc_svc.start_broadcast(bot, session_maker, bc.id, user.language)
    await edit(call, t(user.language, "bc_started"))
