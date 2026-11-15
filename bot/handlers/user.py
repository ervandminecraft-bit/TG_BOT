"""User side: /start, main menu, language, catalog, my subscription, promo, referrals, support."""
from __future__ import annotations

import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Order, OrderStatus, PaymentMethodInfo, Plan, PlanPrice, PromoCode, ReferralReward, User
from bot.i18n import t
from bot.keyboards import LangCB, MenuCB, PlanCB, back_to_menu_kb, main_menu_kb
from bot.services import access as access_svc
from bot.services import promo as promo_svc
from bot.services import referral as ref_svc
from bot.services.common import is_admin
from bot.services.notifications import plan_duration, status_label
from bot.services.subscriptions import is_sub_active, user_subscriptions
from bot.utils.money import fmt_amount
from bot.utils.text import esc
from bot.utils.timeutil import days_left, fmt_dt, utcnow

router = Router(name="user")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

_relink_cooldown: dict[int, float] = {}
RELINK_COOLDOWN_SECONDS = 60


class PromoInput(StatesGroup):
    code = State()


async def show_main_menu(target: Message | CallbackQuery, user: User, edit: bool = False) -> None:
    text = t(user.language, "main_menu", name=esc(user.first_name or ""))
    kb = main_menu_kb(user.language, is_admin(user.id))
    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, kb)
    else:
        await target.answer(text, reply_markup=kb)


async def safe_edit(call: CallbackQuery, text: str, markup=None) -> None:
    """Edit the message under the button; send a new one if editing is impossible (e.g. a photo)."""
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001 - "message is not modified", media messages, too old...
        await call.message.answer(text, reply_markup=markup)
    try:
        await call.answer()
    except Exception:  # noqa: BLE001
        pass


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, session: AsyncSession, user: User,
                    is_new_user: bool, state: FSMContext) -> None:
    await state.clear()
    arg = (command.args or "").strip()
    if arg.startswith("ref_"):
        if await ref_svc.attach_referrer(session, user, arg[4:][:16], is_new_user):
            await session.commit()
            await message.answer(t(user.language, "ref_attached", percent=settings.ref_discount_percent)
                                 if settings.ref_discount_percent else t(user.language, "ref_attached_no_discount"))
    if is_new_user:
        await message.answer(t(user.language, "welcome"))
    await show_main_menu(message, user)


@router.message(Command("menu"))
async def cmd_menu(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    await show_main_menu(message, user)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    await message.answer(t(user.language, "action_cancelled"))
    await show_main_menu(message, user)


@router.callback_query(MenuCB.filter(F.action == "main"))
async def cb_main(call: CallbackQuery, user: User, state: FSMContext) -> None:
    await state.clear()
    await show_main_menu(call, user, edit=True)


# ------------------------------------------------------------------ language
@router.callback_query(MenuCB.filter(F.action == "lang"))
async def cb_lang(call: CallbackQuery, user: User) -> None:
    b = InlineKeyboardBuilder()
    b.button(text="🇷🇺 Русский", callback_data=LangCB(code="ru"))
    b.button(text="🇬🇧 English", callback_data=LangCB(code="en"))
    b.adjust(2)
    await safe_edit(call, t(user.language, "choose_language"), b.as_markup())


@router.callback_query(LangCB.filter())
async def cb_set_lang(call: CallbackQuery, callback_data: LangCB, session: AsyncSession, user: User) -> None:
    if callback_data.code in ("ru", "en"):
        user.language = callback_data.code
        await session.commit()
    await show_main_menu(call, user, edit=True)


# ------------------------------------------------------------------ catalog
async def plan_min_price(session: AsyncSession, plan: Plan) -> str:
    rows = await session.execute(
        select(PlanPrice.amount, PaymentMethodInfo.currency)
        .join(PaymentMethodInfo, PaymentMethodInfo.id == PlanPrice.payment_method_id)
        .where(PlanPrice.plan_id == plan.id, PaymentMethodInfo.is_active.is_(True))
        .order_by(PlanPrice.amount)
    )
    first = rows.first()
    return fmt_amount(first[0], first[1]) if first else ""


@router.callback_query(MenuCB.filter(F.action == "catalog"))
async def cb_catalog(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    lang = user.language
    plans = (await session.scalars(
        select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.id)
    )).all()
    plans = [p for p in plans if any(c.is_active for c in p.channels)]
    if not plans:
        await safe_edit(call, t(lang, "catalog_empty"), back_to_menu_kb(lang))
        return
    b = InlineKeyboardBuilder()
    for plan in plans:
        price = await plan_min_price(session, plan)
        label = f"{plan.name} · {plan_duration(lang, plan)}" + (f" · {price}" if price else "")
        b.button(text=label[:60], callback_data=PlanCB(action="view", plan_id=plan.id))
    b.button(text=t(lang, "btn_main_menu"), callback_data=MenuCB(action="main"))
    b.adjust(1)
    await safe_edit(call, t(lang, "catalog_title"), b.as_markup())


@router.callback_query(PlanCB.filter(F.action == "view"))
async def cb_plan(call: CallbackQuery, callback_data: PlanCB, session: AsyncSession, user: User) -> None:
    lang = user.language
    plan = await session.get(Plan, callback_data.plan_id)
    if plan is None or not plan.is_active:
        await call.answer(t(lang, "plan_unavailable"), show_alert=True)
        return
    rows = await session.execute(
        select(PlanPrice, PaymentMethodInfo)
        .join(PaymentMethodInfo, PaymentMethodInfo.id == PlanPrice.payment_method_id)
        .where(PlanPrice.plan_id == plan.id, PaymentMethodInfo.is_active.is_(True))
        .order_by(PaymentMethodInfo.sort_order, PaymentMethodInfo.id)
    )
    prices = rows.all()
    channels = "\n".join(f"• {esc(c.title)}" + (f" — {esc(c.description)}" if c.description else "")
                         for c in plan.channels if c.is_active)
    text = t(lang, "plan_card", name=esc(plan.name), description=esc(plan.description),
             duration=plan_duration(lang, plan), channels=channels or "—")
    promo_note = ""
    if user.active_promo_id:
        promo = await session.get(PromoCode, user.active_promo_id)
        if promo:
            promo_note = "\n\n" + t(lang, "plan_promo_note", code=esc(promo.code))
    b = InlineKeyboardBuilder()
    for price, method in prices:
        b.button(text=t(lang, "btn_pay_with", method=method.name[:30], price=fmt_amount(price.amount, method.currency)),
                 callback_data=PlanCB(action="buy", plan_id=plan.id, method_id=method.id))
    b.button(text=t(lang, "btn_back"), callback_data=MenuCB(action="catalog"))
    b.adjust(1)
    if not prices:
        text += "\n\n" + t(lang, "plan_no_prices")
    await safe_edit(call, text + promo_note, b.as_markup())


# ------------------------------------------------------------------ my subscription
@router.callback_query(MenuCB.filter(F.action == "mysub"))
async def cb_mysub(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    lang = user.language
    now = utcnow()
    subs = await user_subscriptions(session, user.id)
    lines = [t(lang, "mysub_title")]
    b = InlineKeyboardBuilder()
    any_active = False
    for sub, plan in subs:
        active = is_sub_active(sub, now)
        channels = esc(", ".join(c.title for c in plan.channels)) or "—"
        if active:
            any_active = True
            if sub.expires_at is None:
                lines.append(t(lang, "mysub_item_lifetime", plan=esc(plan.name), channels=channels))
            else:
                lines.append(t(lang, "mysub_item_active", plan=esc(plan.name), channels=channels,
                               until=fmt_dt(sub.expires_at, settings.tz), days=days_left(sub.expires_at, now)))
                if plan.is_active:
                    b.button(text=t(lang, "btn_renew_plan", plan=plan.name[:30]),
                             callback_data=PlanCB(action="view", plan_id=plan.id))
        else:
            lines.append(t(lang, "mysub_item_expired", plan=esc(plan.name),
                           until=fmt_dt(sub.expires_at, settings.tz)))
            if plan.is_active:
                b.button(text=t(lang, "btn_renew_plan", plan=plan.name[:30]),
                         callback_data=PlanCB(action="view", plan_id=plan.id))
    if not subs:
        lines.append(t(lang, "mysub_none"))
    open_orders = (await session.scalars(
        select(Order).where(Order.user_id == user.id, Order.status.in_(OrderStatus.OPEN))
        .order_by(Order.created_at.desc()).limit(5)
    )).all()
    if open_orders:
        lines.append(t(lang, "mysub_orders_title"))
        for order in open_orders:
            lines.append(t(lang, "mysub_order_line", code=order.code, status=status_label(lang, order.status),
                           amount=fmt_amount(order.final_amount, order.currency)))
    if user.bonus_days_balance:
        lines.append(t(lang, "mysub_bonus_balance", days=user.bonus_days_balance))
    if any_active:
        b.button(text=t(lang, "btn_relink"), callback_data=MenuCB(action="relink"))
    b.button(text=t(lang, "btn_catalog"), callback_data=MenuCB(action="catalog"))
    b.button(text=t(lang, "btn_main_menu"), callback_data=MenuCB(action="main"))
    b.adjust(1)
    await safe_edit(call, "\n\n".join(lines), b.as_markup())


@router.callback_query(MenuCB.filter(F.action == "relink"))
async def cb_relink(call: CallbackQuery, session: AsyncSession, user: User, bot: Bot) -> None:
    lang = user.language
    now_mono = time.monotonic()
    if now_mono - _relink_cooldown.get(user.id, 0) < RELINK_COOLDOWN_SECONDS:
        await call.answer(t(lang, "relink_cooldown"), show_alert=True)
        return
    _relink_cooldown[user.id] = now_mono
    await call.answer()
    result = await access_svc.deliver_access(bot, session, user, header_key="access_relink")
    await session.commit()
    if not (result.links or result.already or result.approved_pending or result.failed):
        await call.message.answer(t(lang, "relink_no_access"), reply_markup=back_to_menu_kb(lang))


# ------------------------------------------------------------------ promo
@router.callback_query(MenuCB.filter(F.action == "promo"))
async def cb_promo(call: CallbackQuery, user: User, state: FSMContext) -> None:
    await state.set_state(PromoInput.code)
    await safe_edit(call, t(user.language, "promo_enter"), back_to_menu_kb(user.language))


@router.message(StateFilter(PromoInput.code), F.text)
async def promo_entered(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    lang = user.language
    promo, err = await promo_svc.validate_promo(session, message.text, user.id, utcnow())
    if err:
        await message.answer(t(lang, err), reply_markup=back_to_menu_kb(lang))
        return
    user.active_promo_id = promo.id
    await session.commit()
    await state.clear()
    parts = []
    if promo.kind == "percent" and promo.value:
        parts.append(t(lang, "promo_effect_percent", value=promo.value))
    elif promo.kind == "fixed" and promo.value:
        parts.append(t(lang, "promo_effect_fixed", value=fmt_amount(promo.value, promo.currency)))
    if promo.bonus_days:
        parts.append(t(lang, "promo_effect_days", days=promo.bonus_days))
    plans = ", ".join(esc(p.name) for p in promo.plans) if promo.plans else t(lang, "promo_all_plans")
    await message.answer(t(lang, "promo_applied", code=esc(promo.code), effect=", ".join(parts) or "—", plans=plans),
                         reply_markup=main_menu_kb(lang, is_admin(user.id)))


# ------------------------------------------------------------------ referrals
@router.callback_query(MenuCB.filter(F.action == "ref"))
async def cb_ref(call: CallbackQuery, session: AsyncSession, user: User, bot: Bot) -> None:
    lang = user.language
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=ref_{user.ref_code}"
    invited = await session.scalar(select(func.count()).select_from(User).where(User.referred_by == user.id))
    rewarded = await session.scalar(
        select(func.count()).select_from(ReferralReward).where(ReferralReward.referrer_id == user.id)
    )
    rewards = []
    if settings.ref_bonus_days:
        rewards.append(t(lang, "ref_reward_days", days=settings.ref_bonus_days))
    if settings.ref_discount_percent:
        rewards.append(t(lang, "ref_reward_discount", percent=settings.ref_discount_percent))
    await safe_edit(call, t(lang, "ref_info", link=link, invited=invited or 0, rewarded=rewarded or 0,
                            rewards="\n".join(rewards) or "—"), back_to_menu_kb(lang))


# ------------------------------------------------------------------ support
@router.callback_query(MenuCB.filter(F.action == "support"))
async def cb_support(call: CallbackQuery, user: User) -> None:
    contact = esc(settings.support_contact) if settings.support_contact else t(user.language, "support_not_set")
    await safe_edit(call, t(user.language, "support_text", contact=contact), back_to_menu_kb(user.language))
