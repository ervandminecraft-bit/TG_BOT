"""Buyer side of an order: create, "I paid", receipt upload, cancel."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import PaymentMethodInfo, Plan, User
from bot.handlers.user import safe_edit, show_main_menu
from bot.i18n import t
from bot.keyboards import OrderCB, PlanCB, back_to_menu_kb
from bot.services import orders as order_svc
from bot.services.notifications import buyer_order_kb, render_order_for_buyer, send_admin_cards
from bot.utils.timeutil import utcnow

log = logging.getLogger(__name__)

router = Router(name="orders")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

MAX_PROOF_TEXT = 2000


@router.callback_query(PlanCB.filter(F.action == "buy"))
async def cb_buy(call: CallbackQuery, callback_data: PlanCB, session: AsyncSession, user: User) -> None:
    lang = user.language
    plan = await session.get(Plan, callback_data.plan_id)
    method = await session.get(PaymentMethodInfo, callback_data.method_id)
    if plan is None or method is None:
        await call.answer(t(lang, "order_unavailable"), show_alert=True)
        return
    try:
        draft = await order_svc.create_order(session, user, plan, method, utcnow())
    except order_svc.OrderError as exc:
        await session.rollback()
        await call.answer(t(lang, exc.key, limit=settings.max_open_orders_per_user), show_alert=True)
        return
    await session.commit()
    text = await render_order_for_buyer(session, draft.order, lang)
    if draft.promo_ignored:
        text = t(lang, "order_promo_ignored") + "\n\n" + text
    await safe_edit(call, text, buyer_order_kb(lang, draft.order.id))


@router.callback_query(OrderCB.filter(F.action == "paid"))
async def cb_paid(call: CallbackQuery, callback_data: OrderCB, session: AsyncSession, user: User,
                  state: FSMContext) -> None:
    lang = user.language
    await state.clear()  # e.g. leave the promo-code input, so the receipt is not lost
    order = await order_svc.request_proof(session, callback_data.order_id, user.id, utcnow())
    if order is None:
        await call.answer(t(lang, "order_not_actionable"), show_alert=True)
        return
    await session.commit()
    await call.answer()
    await call.message.answer(t(lang, "proof_request", code=order.code, mb=settings.max_proof_file_mb),
                              reply_markup=back_to_menu_kb(lang))


@router.callback_query(OrderCB.filter(F.action == "cancel"))
async def cb_cancel(call: CallbackQuery, callback_data: OrderCB, session: AsyncSession, user: User) -> None:
    lang = user.language
    if await order_svc.cancel_order(session, callback_data.order_id, user.id, utcnow()):
        await safe_edit(call, t(lang, "order_cancelled"), back_to_menu_kb(lang))
    else:
        await call.answer(t(lang, "order_not_cancellable"), show_alert=True)


def _extract_proof(message: Message) -> tuple[str, str | None, str | None, str | None, int | None] | None:
    """Return (kind, file_id, file_unique_id, text, file_size)."""
    caption = (message.caption or "").strip() or None
    if message.photo:
        photo = message.photo[-1]
        return "photo", photo.file_id, photo.file_unique_id, caption, photo.file_size
    if message.document:
        doc = message.document
        return "document", doc.file_id, doc.file_unique_id, caption, doc.file_size
    if message.text and not message.text.startswith("/"):
        return "text", None, None, message.text.strip(), None
    return None


@router.message(StateFilter(None), F.photo | F.document | F.text)
async def receive_proof(message: Message, session: AsyncSession, user: User, bot: Bot) -> None:
    """Any photo/file/text when an order waits for a receipt; otherwise show the menu."""
    lang = user.language
    now = utcnow()
    order = await order_svc.order_waiting_proof(session, user.id, now)
    if order is None:
        await show_main_menu(message, user)
        return
    proof = _extract_proof(message)
    if proof is None:
        await message.answer(t(lang, "proof_unsupported"))
        return
    kind, file_id, unique_id, text, size = proof
    if size and size > settings.max_proof_file_mb * 1024 * 1024:
        await message.answer(t(lang, "proof_too_big", mb=settings.max_proof_file_mb))
        return
    if text and len(text) > MAX_PROOF_TEXT:
        text = text[:MAX_PROOF_TEXT]
    if not await order_svc.submit_proof(session, order, kind, file_id, unique_id, text, now):
        await session.rollback()
        await message.answer(t(lang, "order_not_actionable"))
        return
    await session.commit()
    await message.answer(t(lang, "proof_received", code=order.code), reply_markup=back_to_menu_kb(lang))
    sent = await send_admin_cards(bot, session, order)
    if not sent:
        log.error("Order %s: no admin received the card (check ADMIN_IDS and that admins pressed /start)", order.code)


@router.message(StateFilter(None))
async def fallback(message: Message, user: User) -> None:
    """Stickers, voice, etc. outside of a dialog."""
    await show_main_menu(message, user)

