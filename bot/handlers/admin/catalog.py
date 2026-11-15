"""Admin: channels, plans (with prices) and payment methods."""
from __future__ import annotations

import re

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Channel, PaymentMethodInfo, Plan, PlanPrice, User
from bot.handlers.admin.common import Ctx, admin_router, answer_pair, ask, edit, on_input, retry
from bot.i18n import t
from bot.keyboards import AdminCB
from bot.services.access import check_bot_rights
from bot.services.common import audit
from bot.services.notifications import plan_duration
from bot.utils.money import fmt_amount, parse_amount
from bot.utils.text import esc

router = admin_router("admin_catalog")

ON, OFF = "✅", "⏸"
CURRENCY_RE = re.compile(r"^[A-Z0-9]{2,10}$")


# ================================================================== channels
async def channels_list(session: AsyncSession, lang: str):
    channels = (await session.scalars(select(Channel).order_by(Channel.id))).all()
    b = InlineKeyboardBuilder()
    for ch in channels:
        b.button(text=f"{ON if ch.is_active else OFF} {ch.title}"[:60], callback_data=AdminCB(s="ch", a="card", i=ch.id))
    b.button(text=t(lang, "adm_btn_add_channel"), callback_data=AdminCB(s="ch", a="add"))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="home"))
    b.adjust(1)
    text = t(lang, "adm_channels_title") + ("" if channels else "\n\n" + t(lang, "adm_empty"))
    return text, b.as_markup()


@router.callback_query(AdminCB.filter((F.s == "ch") & (F.a == "")))
async def cb_channels(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    await edit(call, *await channels_list(session, user.language))


async def channel_card(session: AsyncSession, lang: str, channel: Channel):
    plans = (await session.scalars(select(Plan).where(Plan.channels.any(Channel.id == channel.id)))).all()
    text = t(lang, "adm_channel_card", title=esc(channel.title), chat_id=channel.chat_id,
             description=esc(channel.description) or "—", active=ON if channel.is_active else OFF,
             plans=esc(", ".join(p.name for p in plans)) or "—")
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "adm_btn_edit_desc"), callback_data=AdminCB(s="ch", a="desc", i=channel.id))
    b.button(text=t(lang, "adm_btn_check_rights"), callback_data=AdminCB(s="ch", a="check", i=channel.id))
    b.button(text=t(lang, "adm_btn_toggle"), callback_data=AdminCB(s="ch", a="toggle", i=channel.id))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="ch"))
    b.adjust(1)
    return text, b.as_markup()


@router.callback_query(AdminCB.filter((F.s == "ch") & (F.a == "card")))
async def cb_channel(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    channel = await session.get(Channel, callback_data.i)
    if channel is None:
        await call.answer(t(user.language, "adm_not_found"), show_alert=True)
        return
    await edit(call, *await channel_card(session, user.language, channel))


@router.callback_query(AdminCB.filter((F.s == "ch") & (F.a == "add")))
async def cb_channel_add(call: CallbackQuery, user: User, state: FSMContext) -> None:
    await ask(call, state, user.language, "channel_add", "adm_channel_add_prompt", back=("ch", "", 0))


@on_input("channel_add")
async def in_channel_add(ctx: Ctx) -> None:
    msg = ctx.message
    ref: int | str | None = None
    origin = getattr(msg, "forward_origin", None)
    if origin is not None and getattr(origin, "chat", None) is not None:
        ref = origin.chat.id
    elif msg.text:
        raw = msg.text.strip()
        if re.fullmatch(r"-?\d{5,20}", raw):
            ref = int(raw)
        elif re.fullmatch(r"@?[A-Za-z0-9_]{4,64}", raw):
            ref = raw if raw.startswith("@") else "@" + raw
        elif "t.me/" in raw and "+" not in raw:
            ref = "@" + raw.rstrip("/").split("/")[-1]
    if ref is None:
        await retry(ctx, "adm_channel_bad_ref")
        return
    check = await check_bot_rights(ctx.bot, ref)
    if check.error:
        await retry(ctx, check.error)
        return
    title, chat_id = check.title, check.chat_id
    channel = await ctx.session.scalar(select(Channel).where(Channel.chat_id == chat_id))
    if channel is None:
        channel = Channel(chat_id=chat_id, title=title or str(chat_id))
        ctx.session.add(channel)
    else:
        channel.title = title or channel.title
        channel.is_active = True
    await audit(ctx.session, ctx.user.id, "channel_add", f"channel:{chat_id}", title)
    await ctx.session.commit()
    await ctx.state.clear()
    text, kb = await channel_card(ctx.session, ctx.lang, channel)
    warning = "\n\n" + t(ctx.lang, check.warning) if check.warning else ""
    await msg.answer(t(ctx.lang, "adm_channel_added") + warning + "\n\n" + text, reply_markup=kb)


@router.callback_query(AdminCB.filter((F.s == "ch") & (F.a == "desc")))
async def cb_channel_desc(call: CallbackQuery, callback_data: AdminCB, user: User, state: FSMContext) -> None:
    await ask(call, state, user.language, "channel_desc", "adm_enter_description",
              back=("ch", "card", callback_data.i), channel_id=callback_data.i)


@on_input("channel_desc")
async def in_channel_desc(ctx: Ctx) -> None:
    channel = await ctx.session.get(Channel, int(ctx.data["channel_id"]))
    if channel is None or not ctx.message.text:
        await retry(ctx, "adm_text_required")
        return
    channel.description = "" if ctx.text == "-" else ctx.text[:1000]
    await ctx.session.commit()
    await ctx.state.clear()
    await ctx.message.answer(t(ctx.lang, "adm_saved"))
    await answer_pair(ctx.message, await channel_card(ctx.session, ctx.lang, channel))


@router.callback_query(AdminCB.filter((F.s == "ch") & (F.a == "check")))
async def cb_channel_check(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User, bot: Bot) -> None:
    channel = await session.get(Channel, callback_data.i)
    if channel is None:
        return
    check = await check_bot_rights(bot, channel.chat_id)
    if check.title:
        channel.title = check.title
        await session.commit()
    if check.error or check.warning:
        await call.message.answer(t(user.language, check.error or check.warning))
        await call.answer()
    else:
        await call.answer(t(user.language, "adm_rights_ok"), show_alert=True)
    text, kb = await channel_card(session, user.language, channel)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(AdminCB.filter((F.s == "ch") & (F.a == "toggle")))
async def cb_channel_toggle(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    channel = await session.get(Channel, callback_data.i)
    if channel is None:
        return
    channel.is_active = not channel.is_active
    await audit(session, user.id, "channel_toggle", f"channel:{channel.chat_id}", str(channel.is_active))
    await session.commit()
    await edit(call, *await channel_card(session, user.language, channel))


# ================================================================== plans
async def plans_list(session: AsyncSession, lang: str):
    plans = (await session.scalars(select(Plan).order_by(Plan.sort_order, Plan.id))).all()
    b = InlineKeyboardBuilder()
    for plan in plans:
        b.button(text=f"{ON if plan.is_active else OFF} {plan.name} · {plan_duration(lang, plan)}"[:60],
                 callback_data=AdminCB(s="pl", a="card", i=plan.id))
    b.button(text=t(lang, "adm_btn_add_plan"), callback_data=AdminCB(s="pl", a="add"))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="home"))
    b.adjust(1)
    text = t(lang, "adm_plans_title") + ("" if plans else "\n\n" + t(lang, "adm_empty"))
    return text, b.as_markup()


@router.callback_query(AdminCB.filter((F.s == "pl") & (F.a == "")))
async def cb_plans(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    await edit(call, *await plans_list(session, user.language))


async def plan_card(session: AsyncSession, lang: str, plan: Plan):
    await session.refresh(plan)
    methods = {m.id: m for m in (await session.scalars(select(PaymentMethodInfo))).all()}
    prices = "\n".join(
        f"• {esc(methods[p.payment_method_id].name)}: {fmt_amount(p.amount, methods[p.payment_method_id].currency)}"
        for p in plan.prices if p.payment_method_id in methods
    ) or "—"
    warnings = []
    if not plan.channels:
        warnings.append(t(lang, "adm_plan_warn_channels"))
    if not plan.prices:
        warnings.append(t(lang, "adm_plan_warn_prices"))
    text = t(lang, "adm_plan_card", name=esc(plan.name), description=esc(plan.description) or "—",
             duration=plan_duration(lang, plan), channels=esc(", ".join(c.title for c in plan.channels)) or "—",
             prices=prices, active=ON if plan.is_active else OFF, order=plan.sort_order)
    if warnings:
        text += "\n\n" + "\n".join(warnings)
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "adm_btn_edit_name"), callback_data=AdminCB(s="pl", a="edit", i=plan.id, x="name"))
    b.button(text=t(lang, "adm_btn_edit_desc"), callback_data=AdminCB(s="pl", a="edit", i=plan.id, x="desc"))
    b.button(text=t(lang, "adm_btn_edit_days"), callback_data=AdminCB(s="pl", a="edit", i=plan.id, x="days"))
    b.button(text=t(lang, "adm_btn_plan_channels"), callback_data=AdminCB(s="pl", a="chs", i=plan.id))
    b.button(text=t(lang, "adm_btn_plan_prices"), callback_data=AdminCB(s="pl", a="prices", i=plan.id))
    b.button(text=t(lang, "adm_btn_toggle"), callback_data=AdminCB(s="pl", a="toggle", i=plan.id))
    b.button(text="⬆️", callback_data=AdminCB(s="pl", a="move", i=plan.id, x="up"))
    b.button(text="⬇️", callback_data=AdminCB(s="pl", a="move", i=plan.id, x="down"))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="pl"))
    b.adjust(3, 2, 1, 2, 1)
    return text, b.as_markup()


@router.callback_query(AdminCB.filter((F.s == "pl") & (F.a == "card")))
async def cb_plan(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    plan = await session.get(Plan, callback_data.i)
    if plan is None:
        await call.answer(t(user.language, "adm_not_found"), show_alert=True)
        return
    await edit(call, *await plan_card(session, user.language, plan))


@router.callback_query(AdminCB.filter((F.s == "pl") & (F.a == "add")))
async def cb_plan_add(call: CallbackQuery, user: User, state: FSMContext) -> None:
    await state.update_data(wiz={})
    await ask(call, state, user.language, "plan_new_name", "adm_plan_enter_name", back=("pl", "", 0))


@on_input("plan_new_name")
async def in_plan_new_name(ctx: Ctx) -> None:
    if not ctx.message.text or len(ctx.text) > 128:
        await retry(ctx, "adm_text_required")
        return
    await ask(ctx.message, ctx.state, ctx.lang, "plan_new_desc", "adm_enter_description",
              back=("pl", "", 0), wiz={"name": ctx.text})


@on_input("plan_new_desc")
async def in_plan_new_desc(ctx: Ctx) -> None:
    if not ctx.message.text:
        await retry(ctx, "adm_text_required")
        return
    wiz = dict(ctx.data.get("wiz") or {})
    wiz["desc"] = "" if ctx.text == "-" else ctx.text[:2000]
    await ask(ctx.message, ctx.state, ctx.lang, "plan_new_days", "adm_plan_enter_days", back=("pl", "", 0), wiz=wiz)


def _parse_days(text: str) -> int | None | bool:
    """Return days, None for lifetime (0), or False if invalid."""
    if not text.isdigit():
        return False
    days = int(text)
    if days > 36500:
        return False
    return None if days == 0 else days


@on_input("plan_new_days")
async def in_plan_new_days(ctx: Ctx) -> None:
    days = _parse_days(ctx.text)
    if days is False:
        await retry(ctx, "adm_bad_number")
        return
    wiz = dict(ctx.data.get("wiz") or {})
    max_order = await ctx.session.scalar(select(func.max(Plan.sort_order))) or 0
    plan = Plan(name=wiz.get("name", "Plan"), description=wiz.get("desc", ""), duration_days=days,
                is_active=True, sort_order=max_order + 1)
    ctx.session.add(plan)
    await audit(ctx.session, ctx.user.id, "plan_create", None, plan.name)
    await ctx.session.commit()
    await ctx.state.clear()
    text, kb = await plan_card(ctx.session, ctx.lang, plan)
    await ctx.message.answer(t(ctx.lang, "adm_plan_created") + "\n\n" + text, reply_markup=kb)


@router.callback_query(AdminCB.filter((F.s == "pl") & (F.a == "edit")))
async def cb_plan_edit(call: CallbackQuery, callback_data: AdminCB, user: User, state: FSMContext) -> None:
    prompt = {"name": "adm_plan_enter_name", "desc": "adm_enter_description", "days": "adm_plan_enter_days"}
    field = callback_data.x if callback_data.x in prompt else "name"
    await ask(call, state, user.language, "plan_edit", prompt[field], back=("pl", "card", callback_data.i),
              plan_id=callback_data.i, field=field)


@on_input("plan_edit")
async def in_plan_edit(ctx: Ctx) -> None:
    plan = await ctx.session.get(Plan, int(ctx.data["plan_id"]))
    field = ctx.data.get("field")
    if plan is None or not ctx.message.text:
        await retry(ctx, "adm_text_required")
        return
    if field == "name":
        if len(ctx.text) > 128:
            await retry(ctx, "adm_text_required")
            return
        plan.name = ctx.text
    elif field == "desc":
        plan.description = "" if ctx.text == "-" else ctx.text[:2000]
    else:
        days = _parse_days(ctx.text)
        if days is False:
            await retry(ctx, "adm_bad_number")
            return
        plan.duration_days = days
    await audit(ctx.session, ctx.user.id, "plan_edit", f"plan:{plan.id}", f"{field}")
    await ctx.session.commit()
    await ctx.state.clear()
    await answer_pair(ctx.message, await plan_card(ctx.session, ctx.lang, plan))


@router.callback_query(AdminCB.filter((F.s == "pl") & (F.a.in_({"chs", "cht"}))))
async def cb_plan_channels(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    lang = user.language
    plan = await session.get(Plan, callback_data.i)
    if plan is None:
        return
    if callback_data.a == "cht" and callback_data.x.isdigit():
        channel = await session.get(Channel, int(callback_data.x))
        if channel is not None:
            if any(c.id == channel.id for c in plan.channels):
                plan.channels = [c for c in plan.channels if c.id != channel.id]
            else:
                plan.channels = [*plan.channels, channel]
            await audit(session, user.id, "plan_channels", f"plan:{plan.id}", ",".join(str(c.id) for c in plan.channels))
            await session.commit()
            await session.refresh(plan)
    selected = {c.id for c in plan.channels}
    channels = (await session.scalars(select(Channel).order_by(Channel.id))).all()
    b = InlineKeyboardBuilder()
    for ch in channels:
        mark = "☑️" if ch.id in selected else "⬜"
        b.button(text=f"{mark} {ch.title}"[:60], callback_data=AdminCB(s="pl", a="cht", i=plan.id, x=str(ch.id)))
    b.button(text=t(lang, "btn_done"), callback_data=AdminCB(s="pl", a="card", i=plan.id))
    b.adjust(1)
    text = t(lang, "adm_plan_channels", name=esc(plan.name))
    if not channels:
        text += "\n\n" + t(lang, "adm_no_channels_yet")
    await edit(call, text, b.as_markup())


@router.callback_query(AdminCB.filter((F.s == "pl") & (F.a == "prices")))
async def cb_plan_prices(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    lang = user.language
    plan = await session.get(Plan, callback_data.i)
    if plan is None:
        return
    await session.refresh(plan)
    current = {p.payment_method_id: p.amount for p in plan.prices}
    methods = (await session.scalars(select(PaymentMethodInfo).order_by(PaymentMethodInfo.sort_order,
                                                                        PaymentMethodInfo.id))).all()
    b = InlineKeyboardBuilder()
    for m in methods:
        price = fmt_amount(current[m.id], m.currency) if m.id in current else "—"
        b.button(text=f"{ON if m.is_active else OFF} {m.name}: {price}"[:60],
                 callback_data=AdminCB(s="pl", a="price", i=plan.id, x=str(m.id)))
    b.button(text=t(lang, "btn_done"), callback_data=AdminCB(s="pl", a="card", i=plan.id))
    b.adjust(1)
    text = t(lang, "adm_plan_prices", name=esc(plan.name))
    if not methods:
        text += "\n\n" + t(lang, "adm_no_methods_yet")
    await edit(call, text, b.as_markup())


@router.callback_query(AdminCB.filter((F.s == "pl") & (F.a == "price")))
async def cb_plan_price(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User,
                        state: FSMContext) -> None:
    method = await session.get(PaymentMethodInfo, int(callback_data.x or 0))
    if method is None:
        return
    await ask(call, state, user.language, "plan_price", "adm_enter_price", back=("pl", "prices", callback_data.i),
              plan_id=callback_data.i, method_id=method.id, currency=method.currency, method=method.name)


@on_input("plan_price")
async def in_plan_price(ctx: Ctx) -> None:
    plan_id, method_id = int(ctx.data["plan_id"]), int(ctx.data["method_id"])
    price = await ctx.session.scalar(select(PlanPrice).where(PlanPrice.plan_id == plan_id,
                                                             PlanPrice.payment_method_id == method_id))
    if ctx.text == "-":
        if price is not None:
            await ctx.session.delete(price)
    else:
        amount = parse_amount(ctx.text)
        if amount is None or amount <= 0:
            await retry(ctx, "adm_bad_amount")
            return
        if price is None:
            ctx.session.add(PlanPrice(plan_id=plan_id, payment_method_id=method_id, amount=amount))
        else:
            price.amount = amount
    await audit(ctx.session, ctx.user.id, "plan_price", f"plan:{plan_id}", f"method={method_id} value={ctx.text}")
    await ctx.session.commit()
    await ctx.state.clear()
    plan = await ctx.session.get(Plan, plan_id)
    await answer_pair(ctx.message, await plan_card(ctx.session, ctx.lang, plan))


@router.callback_query(AdminCB.filter((F.s == "pl") & (F.a == "toggle")))
async def cb_plan_toggle(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    plan = await session.get(Plan, callback_data.i)
    if plan is None:
        return
    plan.is_active = not plan.is_active
    await audit(session, user.id, "plan_toggle", f"plan:{plan.id}", str(plan.is_active))
    await session.commit()
    await edit(call, *await plan_card(session, user.language, plan))


@router.callback_query(AdminCB.filter((F.s == "pl") & (F.a == "move")))
async def cb_plan_move(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    plans = list((await session.scalars(select(Plan).order_by(Plan.sort_order, Plan.id))).all())
    idx = next((n for n, p in enumerate(plans) if p.id == callback_data.i), None)
    if idx is None:
        return
    swap = idx - 1 if callback_data.x == "up" else idx + 1
    if 0 <= swap < len(plans):
        plans[idx], plans[swap] = plans[swap], plans[idx]
    for n, p in enumerate(plans):
        p.sort_order = n
    await session.commit()
    await edit(call, *await plan_card(session, user.language, plans[swap if 0 <= swap < len(plans) else idx]))


# ================================================================== payment methods
async def methods_list(session: AsyncSession, lang: str):
    methods = (await session.scalars(select(PaymentMethodInfo).order_by(PaymentMethodInfo.sort_order,
                                                                        PaymentMethodInfo.id))).all()
    b = InlineKeyboardBuilder()
    for m in methods:
        b.button(text=f"{ON if m.is_active else OFF} {m.name} ({m.currency})"[:60],
                 callback_data=AdminCB(s="pm", a="card", i=m.id))
    b.button(text=t(lang, "adm_btn_add_method"), callback_data=AdminCB(s="pm", a="add"))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="home"))
    b.adjust(1)
    text = t(lang, "adm_methods_title") + ("" if methods else "\n\n" + t(lang, "adm_empty"))
    return text, b.as_markup()


@router.callback_query(AdminCB.filter((F.s == "pm") & (F.a == "")))
async def cb_methods(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    await edit(call, *await methods_list(session, user.language))


def method_card(lang: str, m: PaymentMethodInfo):
    text = t(lang, "adm_method_card", name=esc(m.name), currency=esc(m.currency), details=esc(m.details) or "—",
             active=ON if m.is_active else OFF)
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "adm_btn_edit_name"), callback_data=AdminCB(s="pm", a="edit", i=m.id, x="name"))
    b.button(text=t(lang, "adm_btn_edit_currency"), callback_data=AdminCB(s="pm", a="edit", i=m.id, x="currency"))
    b.button(text=t(lang, "adm_btn_edit_details"), callback_data=AdminCB(s="pm", a="edit", i=m.id, x="details"))
    b.button(text=t(lang, "adm_btn_toggle"), callback_data=AdminCB(s="pm", a="toggle", i=m.id))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="pm"))
    b.adjust(2, 1, 1, 1)
    return text, b.as_markup()


@router.callback_query(AdminCB.filter((F.s == "pm") & (F.a == "card")))
async def cb_method(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    m = await session.get(PaymentMethodInfo, callback_data.i)
    if m is None:
        await call.answer(t(user.language, "adm_not_found"), show_alert=True)
        return
    await edit(call, *method_card(user.language, m))


@router.callback_query(AdminCB.filter((F.s == "pm") & (F.a == "add")))
async def cb_method_add(call: CallbackQuery, user: User, state: FSMContext) -> None:
    await state.update_data(wiz={})
    await ask(call, state, user.language, "pm_new_name", "adm_method_enter_name", back=("pm", "", 0))


@on_input("pm_new_name")
async def in_pm_name(ctx: Ctx) -> None:
    if not ctx.message.text or len(ctx.text) > 64:
        await retry(ctx, "adm_text_required")
        return
    await ask(ctx.message, ctx.state, ctx.lang, "pm_new_currency", "adm_method_enter_currency",
              back=("pm", "", 0), wiz={"name": ctx.text})


@on_input("pm_new_currency")
async def in_pm_currency(ctx: Ctx) -> None:
    cur = ctx.text.upper()
    if not CURRENCY_RE.match(cur):
        await retry(ctx, "adm_bad_currency")
        return
    wiz = dict(ctx.data.get("wiz") or {})
    wiz["currency"] = cur
    await ask(ctx.message, ctx.state, ctx.lang, "pm_new_details", "adm_method_enter_details", back=("pm", "", 0), wiz=wiz)


@on_input("pm_new_details")
async def in_pm_details(ctx: Ctx) -> None:
    if not ctx.message.text:
        await retry(ctx, "adm_text_required")
        return
    wiz = dict(ctx.data.get("wiz") or {})
    max_order = await ctx.session.scalar(select(func.max(PaymentMethodInfo.sort_order))) or 0
    m = PaymentMethodInfo(name=wiz.get("name", "Method"), currency=wiz.get("currency", "RUB"),
                          details=ctx.text[:3000], provider="manual", sort_order=max_order + 1)
    ctx.session.add(m)
    await audit(ctx.session, ctx.user.id, "method_create", None, f"{m.name} {m.currency}")
    await ctx.session.commit()
    await ctx.state.clear()
    text, kb = method_card(ctx.lang, m)
    await ctx.message.answer(t(ctx.lang, "adm_method_created") + "\n\n" + text, reply_markup=kb)


@router.callback_query(AdminCB.filter((F.s == "pm") & (F.a == "edit")))
async def cb_method_edit(call: CallbackQuery, callback_data: AdminCB, user: User, state: FSMContext) -> None:
    prompt = {"name": "adm_method_enter_name", "currency": "adm_method_enter_currency",
              "details": "adm_method_enter_details"}
    field = callback_data.x if callback_data.x in prompt else "name"
    await ask(call, state, user.language, "pm_edit", prompt[field], back=("pm", "card", callback_data.i),
              method_id=callback_data.i, field=field)


@on_input("pm_edit")
async def in_pm_edit(ctx: Ctx) -> None:
    m = await ctx.session.get(PaymentMethodInfo, int(ctx.data["method_id"]))
    field = ctx.data.get("field")
    if m is None or not ctx.message.text:
        await retry(ctx, "adm_text_required")
        return
    if field == "currency":
        cur = ctx.text.upper()
        if not CURRENCY_RE.match(cur):
            await retry(ctx, "adm_bad_currency")
            return
        m.currency = cur
    elif field == "details":
        m.details = ctx.text[:3000]
    else:
        if len(ctx.text) > 64:
            await retry(ctx, "adm_text_required")
            return
        m.name = ctx.text
    await audit(ctx.session, ctx.user.id, "method_edit", f"method:{m.id}", field)
    await ctx.session.commit()
    await ctx.state.clear()
    await answer_pair(ctx.message, method_card(ctx.lang, m))


@router.callback_query(AdminCB.filter((F.s == "pm") & (F.a == "toggle")))
async def cb_method_toggle(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    m = await session.get(PaymentMethodInfo, callback_data.i)
    if m is None:
        return
    m.is_active = not m.is_active
    await audit(session, user.id, "method_toggle", f"method:{m.id}", str(m.is_active))
    await session.commit()
    await edit(call, *method_card(user.language, m))
