"""Admin decisions on order cards: approve / reject (template or custom reason)."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Order, Plan, User
from bot.filters import IsAdmin
from bot.i18n import t
from bot.keyboards import MenuCB, OrderCB, ReviewCB
from bot.services import orders as order_svc
from bot.services.access import grant_order_access
from bot.services.common import safe_send
from bot.services.notifications import (
    admin_name,
    reject_kb,
    reject_reason_text,
    review_kb,
    update_admin_cards,
)
from bot.utils.text import esc
from bot.utils.timeutil import fmt_dt, utcnow

log = logging.getLogger(__name__)

router = Router(name="review")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class RejectInput(StatesGroup):
    reason = State()


@router.callback_query(ReviewCB.filter(F.action == "ok"))
async def cb_approve(call: CallbackQuery, callback_data: ReviewCB, session: AsyncSession, user: User, bot: Bot) -> None:
    lang = user.language
    now = utcnow()
    result = await order_svc.approve_order(session, callback_data.order_id, user.id, now)
    if result.status != "ok":
        await call.answer(t(lang, "review_already_done"), show_alert=True)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        return
    await call.answer(t(lang, "review_approved_toast"))
    order = result.order
    await update_admin_cards(bot, session, order, "card_status_approved", admin=admin_name(user),
                             time=fmt_dt(now, settings.tz))
    await after_approve(bot, session, result)


async def after_approve(bot: Bot, session: AsyncSession, result: order_svc.ApproveResult) -> None:
    """Notify the buyer, deliver access, notify the referrer. Used by the card and by admin panel."""
    order = result.order
    buyer = await session.get(User, order.user_id)
    plan = await session.get(Plan, order.plan_id)
    sub = result.subscription
    until = t(buyer.language, "duration_lifetime") if sub.expires_at is None else fmt_dt(sub.expires_at, settings.tz)
    await safe_send(bot, session, buyer.id, t(buyer.language, "order_approved_user", code=order.code,
                                              plan=esc(plan.name), until=until))
    await session.commit()
    await grant_order_access(bot, session, order.id)
    if result.reward:
        referrer = await session.get(User, result.reward.referrer_id)
        if referrer:
            key = "ref_reward_applied_sub" if result.reward.applied_to == "subscription" else "ref_reward_applied_balance"
            await safe_send(bot, session, referrer.id, t(referrer.language, key, days=result.reward.days))
            await session.commit()


@router.callback_query(ReviewCB.filter(F.action == "no"))
async def cb_reject_menu(call: CallbackQuery, callback_data: ReviewCB, session: AsyncSession, user: User) -> None:
    order = await session.get(Order, callback_data.order_id)
    if order is None or order.status != "pending_review":
        await call.answer(t(user.language, "review_already_done"), show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=reject_kb(user.language, order.id))
    await call.answer()


@router.callback_query(ReviewCB.filter(F.action == "back"))
async def cb_reject_back(call: CallbackQuery, callback_data: ReviewCB, user: User) -> None:
    await call.message.edit_reply_markup(reply_markup=review_kb(user.language, callback_data.order_id))
    await call.answer()


@router.callback_query(ReviewCB.filter(F.action == "tpl"))
async def cb_reject_tpl(call: CallbackQuery, callback_data: ReviewCB, session: AsyncSession, user: User, bot: Bot) -> None:
    reason = "" if callback_data.extra == "-" else f"tpl:{callback_data.extra}"
    await do_reject(call, session, user, bot, callback_data.order_id, reason)


@router.callback_query(ReviewCB.filter(F.action == "custom"))
async def cb_reject_custom(call: CallbackQuery, callback_data: ReviewCB, user: User, state: FSMContext) -> None:
    await state.set_state(RejectInput.reason)
    await state.update_data(order_id=callback_data.order_id)
    await call.message.answer(t(user.language, "review_enter_reason"))
    await call.answer()


@router.message(StateFilter(RejectInput.reason), F.text)
async def reject_reason_entered(message: Message, session: AsyncSession, user: User, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if message.text.startswith("/"):
        await message.answer(t(user.language, "action_cancelled"))
        return
    await do_reject(message, session, user, bot, int(data.get("order_id", 0)), message.text.strip()[:500])


async def do_reject(target: CallbackQuery | Message, session: AsyncSession, admin: User, bot: Bot,
                    order_id: int, reason: str) -> None:
    lang = admin.language
    now = utcnow()
    result = await order_svc.reject_order(session, order_id, admin.id, reason, now)
    if result.status != "ok":
        if isinstance(target, CallbackQuery):
            await target.answer(t(lang, "review_already_done"), show_alert=True)
        else:
            await target.answer(t(lang, "review_already_done"))
        return
    if isinstance(target, CallbackQuery):
        await target.answer(t(lang, "review_rejected_toast"))
    else:
        await target.answer(t(lang, "review_rejected_toast"))
    order = result.order
    await update_admin_cards(bot, session, order, "card_status_rejected", admin=admin_name(admin),
                             time=fmt_dt(now, settings.tz), reason=reject_reason_text(lang, reason))
    buyer = await session.get(User, order.user_id)
    b = InlineKeyboardBuilder()
    b.button(text=t(buyer.language, "btn_resend_proof"), callback_data=OrderCB(action="paid", order_id=order.id))
    b.button(text=t(buyer.language, "btn_main_menu"), callback_data=MenuCB(action="main"))
    b.adjust(1)
    await safe_send(bot, session, buyer.id,
                    t(buyer.language, "order_rejected_user", code=order.code,
                      reason=reject_reason_text(buyer.language, reason), hours=settings.order_ttl_hours),
                    reply_markup=b.as_markup())
    await session.commit()

