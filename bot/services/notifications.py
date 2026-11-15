"""Rendering and sending of order-related messages (buyer and admin cards)."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup, ReplyParameters
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Order, OrderAdminMessage, OrderStatus, PaymentMethodInfo, Plan, PromoCode, User
from bot.i18n import t
from bot.keyboards import OrderCB, ReviewCB
from bot.services.common import admin_languages
from bot.utils.money import fmt_amount
from bot.utils.text import esc, truncate, user_display
from bot.utils.timeutil import fmt_dt

log = logging.getLogger(__name__)

REJECT_TEMPLATES = ("not_found", "wrong_amount", "unreadable", "wrong_code")


def plan_duration(lang: str, plan: Plan) -> str:
    if plan.duration_days is None:
        return t(lang, "duration_lifetime")
    return t(lang, "duration_days", days=plan.duration_days)


def status_label(lang: str, status: str) -> str:
    return t(lang, f"status_{status}")


def reject_reason_text(lang: str, reason: str | None) -> str:
    if not reason:
        return t(lang, "rej_no_reason")
    if reason.startswith("tpl:"):
        return t(lang, f"rej_tpl_{reason[4:]}")
    return esc(reason)


async def _order_bundle(session: AsyncSession, order: Order) -> tuple[User, Plan, PaymentMethodInfo, PromoCode | None]:
    user = await session.get(User, order.user_id)
    plan = await session.get(Plan, order.plan_id)
    method = await session.get(PaymentMethodInfo, order.payment_method_id)
    promo = await session.get(PromoCode, order.promo_id) if order.promo_id else None
    return user, plan, method, promo


def amount_block(lang: str, order: Order, promo: PromoCode | None) -> str:
    lines = [t(lang, "order_amount", amount=fmt_amount(order.final_amount, order.currency))]
    if order.discount_amount:
        source = (t(lang, "order_discount_promo", code=esc(promo.code)) if promo and not order.referral_discount
                  else t(lang, "order_discount_ref"))
        lines.append(t(lang, "order_discount", base=fmt_amount(order.base_amount, order.currency),
                       discount=fmt_amount(order.discount_amount, order.currency), source=source))
    if order.bonus_days:
        lines.append(t(lang, "order_bonus_days", days=order.bonus_days))
    return "\n".join(lines)


async def render_order_for_buyer(session: AsyncSession, order: Order, lang: str) -> str:
    user, plan, method, promo = await _order_bundle(session, order)
    return t(lang, "order_created",
             code=order.code, plan=esc(plan.name), duration=plan_duration(lang, plan),
             amount_block=amount_block(lang, order, promo), method=esc(method.name),
             details=esc(method.details), hours=settings.order_ttl_hours)


def buyer_order_kb(lang: str, order_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "btn_paid"), callback_data=OrderCB(action="paid", order_id=order_id))
    b.button(text=t(lang, "btn_cancel_order"), callback_data=OrderCB(action="cancel", order_id=order_id))
    b.adjust(1)
    return b.as_markup()


async def render_admin_card(session: AsyncSession, order: Order, lang: str, dups: list[str] | None = None) -> str:
    user, plan, method, promo = await _order_bundle(session, order)
    tz = settings.tz
    lines = [
        t(lang, "card_title", code=order.code, status=status_label(lang, order.status)),
        t(lang, "card_user", user=user_display(user.first_name, user.last_name, user.username, user.id)),
        t(lang, "card_plan", plan=esc(plan.name), duration=plan_duration(lang, plan)),
        t(lang, "card_channels", channels=esc(", ".join(c.title for c in plan.channels) or "—")),
        amount_block(lang, order, promo),
        t(lang, "card_method", method=esc(method.name), currency=esc(order.currency)),
        t(lang, "card_times", created=fmt_dt(order.created_at, tz), proof=fmt_dt(order.proof_submitted_at, tz)),
    ]
    if order.proof_text:
        lines.append(t(lang, "card_proof_text", text=esc(truncate(order.proof_text, 500))))
    if order.proof_type in ("photo", "document"):
        lines.append(t(lang, "card_proof_attached"))
    if dups:
        lines.append(t(lang, "card_duplicate", codes=", ".join(f"#{c}" for c in dups)))
    lines.append(t(lang, "card_check_money"))
    return "\n".join(lines)


def review_kb(lang: str, order_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "btn_approve"), callback_data=ReviewCB(action="ok", order_id=order_id))
    b.button(text=t(lang, "btn_reject"), callback_data=ReviewCB(action="no", order_id=order_id))
    b.adjust(2)
    return b.as_markup()


def reject_kb(lang: str, order_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key in REJECT_TEMPLATES:
        b.button(text=t(lang, f"rej_tpl_{key}"), callback_data=ReviewCB(action="tpl", order_id=order_id, extra=key))
    b.button(text=t(lang, "btn_reject_custom"), callback_data=ReviewCB(action="custom", order_id=order_id))
    b.button(text=t(lang, "btn_reject_noreason"), callback_data=ReviewCB(action="tpl", order_id=order_id, extra="-"))
    b.button(text=t(lang, "btn_back"), callback_data=ReviewCB(action="back", order_id=order_id))
    b.adjust(1)
    return b.as_markup()


async def send_admin_cards(bot: Bot, session: AsyncSession, order: Order, only_admin: int | None = None) -> int:
    """Send the order card (with the receipt) to every admin, or to one admin (resend)."""
    from bot.services.orders import find_duplicate_codes

    dups = await find_duplicate_codes(session, order)
    langs = await admin_languages(session)
    if only_admin is not None:
        langs = {only_admin: langs.get(only_admin, settings.default_language)}
    sent = 0
    for admin_id, lang in langs.items():
        try:
            reply_to = None
            if order.proof_type == "photo" and order.proof_file_id:
                reply_to = (await bot.send_photo(admin_id, order.proof_file_id)).message_id
            elif order.proof_type == "document" and order.proof_file_id:
                reply_to = (await bot.send_document(admin_id, order.proof_file_id)).message_id
            text = await render_admin_card(session, order, lang, dups)
            markup = review_kb(lang, order.id) if order.status == OrderStatus.PENDING_REVIEW else None
            msg = await bot.send_message(
                admin_id, text, reply_markup=markup,
                reply_parameters=ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None,
            )
            session.add(OrderAdminMessage(order_id=order.id, chat_id=admin_id, message_id=msg.message_id))
            sent += 1
        except TelegramAPIError as exc:
            log.warning("Cannot send order card to admin %s: %s", admin_id, exc)
    await session.commit()
    return sent


async def update_admin_cards(bot: Bot, session: AsyncSession, order: Order, status_key: str, **fmt) -> None:
    """After a decision: rewrite every admin's card with the result and remove the buttons."""
    rows = (await session.scalars(select(OrderAdminMessage).where(OrderAdminMessage.order_id == order.id))).all()
    langs = await admin_languages(session)
    for row in rows:
        lang = langs.get(row.chat_id, settings.default_language)
        text = await render_admin_card(session, order, lang)
        text += "\n\n" + t(lang, status_key, **fmt)
        try:
            await bot.edit_message_text(text=text, chat_id=row.chat_id, message_id=row.message_id, reply_markup=None)
        except TelegramAPIError as exc:
            log.debug("Cannot edit admin card %s/%s: %s", row.chat_id, row.message_id, exc)


def admin_name(user) -> str:
    if user is None:
        return "—"
    return esc(f"@{user.username}" if user.username else (user.first_name or str(user.id)))
