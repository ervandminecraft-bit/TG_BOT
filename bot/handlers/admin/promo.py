"""Admin: promo codes (create wizard, list, (de)activate)."""
from __future__ import annotations

from datetime import timedelta

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Plan, PromoCode, User
from bot.handlers.admin.common import Ctx, admin_router, ask, edit, on_input, retry
from bot.i18n import t
from bot.keyboards import AdminCB
from bot.services.common import audit
from bot.services.promo import CODE_RE, normalize_code
from bot.utils.money import fmt_amount, parse_amount
from bot.utils.text import esc
from bot.utils.timeutil import fmt_dt, utcnow

router = admin_router("admin_promo")


def promo_summary(lang: str, p: PromoCode) -> str:
    if p.kind == "percent":
        effect = f"-{p.value}%"
    elif p.kind == "fixed":
        effect = f"-{fmt_amount(p.value, p.currency)}"
    else:
        effect = t(lang, "promo_kind_none")
    if p.bonus_days:
        effect += f", +{p.bonus_days} " + t(lang, "days_short")
    return effect


@router.callback_query(AdminCB.filter((F.s == "promo") & (F.a == "")))
async def cb_promo_list(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    lang = user.language
    promos = (await session.scalars(select(PromoCode).order_by(PromoCode.is_active.desc(), PromoCode.id.desc())
                                    .limit(30))).all()
    b = InlineKeyboardBuilder()
    for p in promos:
        uses = f"{p.used_count}/{p.max_uses}" if p.max_uses else f"{p.used_count}"
        b.button(text=f"{'✅' if p.is_active else '⏸'} {p.code} · {promo_summary(lang, p)} · {uses}"[:60],
                 callback_data=AdminCB(s="promo", a="card", i=p.id))
    b.button(text=t(lang, "adm_btn_add_promo"), callback_data=AdminCB(s="promo", a="add"))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="home"))
    b.adjust(1)
    await edit(call, t(lang, "adm_promo_title") + ("" if promos else "\n\n" + t(lang, "adm_empty")), b.as_markup())


def promo_card(lang: str, p: PromoCode):
    text = t(lang, "adm_promo_card", code=esc(p.code), effect=promo_summary(lang, p),
             until=fmt_dt(p.valid_until, settings.tz) if p.valid_until else t(lang, "promo_no_limit"),
             uses=f"{p.used_count}/{p.max_uses}" if p.max_uses else f"{p.used_count}/∞",
             plans=esc(", ".join(pl.name for pl in p.plans)) if p.plans else t(lang, "promo_all_plans"),
             once=t(lang, "yes") if p.once_per_user else t(lang, "no"),
             active="✅" if p.is_active else "⏸")
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "adm_btn_toggle"), callback_data=AdminCB(s="promo", a="toggle", i=p.id))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="promo"))
    b.adjust(1)
    return text, b.as_markup()


@router.callback_query(AdminCB.filter((F.s == "promo") & (F.a == "card")))
async def cb_promo_card(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    p = await session.get(PromoCode, callback_data.i)
    if p is None:
        await call.answer(t(user.language, "adm_not_found"), show_alert=True)
        return
    await edit(call, *promo_card(user.language, p))


@router.callback_query(AdminCB.filter((F.s == "promo") & (F.a == "toggle")))
async def cb_promo_toggle(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    p = await session.get(PromoCode, callback_data.i)
    if p is None:
        return
    p.is_active = not p.is_active
    await audit(session, user.id, "promo_toggle", f"promo:{p.code}", str(p.is_active))
    await session.commit()
    await edit(call, *promo_card(user.language, p))


# ------------------------------------------------------------------ create wizard
@router.callback_query(AdminCB.filter((F.s == "promo") & (F.a == "add")))
async def cb_promo_add(call: CallbackQuery, user: User, state: FSMContext) -> None:
    await state.update_data(wiz={})
    await ask(call, state, user.language, "promo_code", "adm_promo_enter_code", back=("promo", "", 0))


@on_input("promo_code")
async def in_promo_code(ctx: Ctx) -> None:
    code = normalize_code(ctx.text)
    if not CODE_RE.match(code):
        await retry(ctx, "adm_promo_bad_code")
        return
    if await ctx.session.scalar(select(PromoCode.id).where(PromoCode.code == code)):
        await retry(ctx, "adm_promo_exists")
        return
    await ctx.state.set_state(None)
    await ctx.state.update_data(wiz={"code": code, "plans": []})
    b = InlineKeyboardBuilder()
    for kind in ("percent", "fixed", "none"):
        b.button(text=t(ctx.lang, f"promo_kind_btn_{kind}"), callback_data=AdminCB(s="promo", a="kind", x=kind))
    b.button(text=t(ctx.lang, "btn_back"), callback_data=AdminCB(s="promo"))
    b.adjust(1)
    await ctx.message.answer(t(ctx.lang, "adm_promo_choose_kind", code=esc(code)), reply_markup=b.as_markup())


@router.callback_query(AdminCB.filter((F.s == "promo") & (F.a == "kind")))
async def cb_promo_kind(call: CallbackQuery, callback_data: AdminCB, user: User, state: FSMContext) -> None:
    data = await state.get_data()
    wiz = dict(data.get("wiz") or {})
    if not wiz.get("code"):
        await call.answer(t(user.language, "adm_wizard_lost"), show_alert=True)
        return
    wiz["kind"] = callback_data.x
    if callback_data.x == "percent":
        await ask(call, state, user.language, "promo_value", "adm_promo_enter_percent", back=("promo", "", 0), wiz=wiz)
    elif callback_data.x == "fixed":
        await ask(call, state, user.language, "promo_value", "adm_promo_enter_fixed", back=("promo", "", 0), wiz=wiz)
    else:
        wiz["value"] = 0
        await ask(call, state, user.language, "promo_days", "adm_promo_enter_days", back=("promo", "", 0), wiz=wiz)


@on_input("promo_value")
async def in_promo_value(ctx: Ctx) -> None:
    wiz = dict(ctx.data.get("wiz") or {})
    if wiz.get("kind") == "percent":
        if not ctx.text.isdigit() or not 1 <= int(ctx.text) <= 100:
            await retry(ctx, "adm_bad_percent")
            return
        wiz["value"] = int(ctx.text)
    else:
        parts = ctx.text.split()
        amount = parse_amount(parts[0]) if parts else None
        if len(parts) != 2 or amount is None or amount <= 0:
            await retry(ctx, "adm_promo_bad_fixed")
            return
        wiz["value"], wiz["currency"] = amount, parts[1].upper()[:10]
    await ask(ctx.message, ctx.state, ctx.lang, "promo_days", "adm_promo_enter_days", back=("promo", "", 0), wiz=wiz)


@on_input("promo_days")
async def in_promo_days(ctx: Ctx) -> None:
    if not ctx.text.isdigit() or int(ctx.text) > 3650:
        await retry(ctx, "adm_bad_number")
        return
    wiz = dict(ctx.data.get("wiz") or {})
    wiz["bonus_days"] = int(ctx.text)
    if wiz.get("kind") == "none" and wiz["bonus_days"] == 0:
        await retry(ctx, "adm_promo_empty")
        return
    await ask(ctx.message, ctx.state, ctx.lang, "promo_valid", "adm_promo_enter_valid", back=("promo", "", 0), wiz=wiz)


@on_input("promo_valid")
async def in_promo_valid(ctx: Ctx) -> None:
    if not ctx.text.isdigit() or int(ctx.text) > 3650:
        await retry(ctx, "adm_bad_number")
        return
    wiz = dict(ctx.data.get("wiz") or {})
    wiz["valid_days"] = int(ctx.text)
    await ask(ctx.message, ctx.state, ctx.lang, "promo_uses", "adm_promo_enter_uses", back=("promo", "", 0), wiz=wiz)


@on_input("promo_uses")
async def in_promo_uses(ctx: Ctx) -> None:
    if not ctx.text.isdigit() or int(ctx.text) > 1_000_000:
        await retry(ctx, "adm_bad_number")
        return
    wiz = dict(ctx.data.get("wiz") or {})
    wiz["max_uses"] = int(ctx.text)
    await ctx.state.set_state(None)
    await ctx.state.update_data(wiz=wiz)
    text, kb = await plans_picker(ctx.session, ctx.lang, wiz)
    await ctx.message.answer(text, reply_markup=kb)


async def plans_picker(session: AsyncSession, lang: str, wiz: dict):
    selected = set(wiz.get("plans") or [])
    plans = (await session.scalars(select(Plan).order_by(Plan.sort_order, Plan.id))).all()
    b = InlineKeyboardBuilder()
    for plan in plans:
        mark = "☑️" if plan.id in selected else "⬜"
        b.button(text=f"{mark} {plan.name}"[:60], callback_data=AdminCB(s="promo", a="pt", x=str(plan.id)))
    b.button(text=t(lang, "btn_next"), callback_data=AdminCB(s="promo", a="once"))
    b.adjust(1)
    return t(lang, "adm_promo_choose_plans"), b.as_markup()


@router.callback_query(AdminCB.filter((F.s == "promo") & (F.a == "pt")))
async def cb_promo_plan_toggle(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User,
                               state: FSMContext) -> None:
    wiz = dict((await state.get_data()).get("wiz") or {})
    if not wiz.get("code"):
        await call.answer(t(user.language, "adm_wizard_lost"), show_alert=True)
        return
    selected = set(wiz.get("plans") or [])
    pid = int(callback_data.x)
    selected.symmetric_difference_update({pid})
    wiz["plans"] = sorted(selected)
    await state.update_data(wiz=wiz)
    await edit(call, *await plans_picker(session, user.language, wiz))


@router.callback_query(AdminCB.filter((F.s == "promo") & (F.a == "once")))
async def cb_promo_once(call: CallbackQuery, user: User) -> None:
    lang = user.language
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "promo_once_yes"), callback_data=AdminCB(s="promo", a="save", x="1"))
    b.button(text=t(lang, "promo_once_no"), callback_data=AdminCB(s="promo", a="save", x="0"))
    b.adjust(1)
    await edit(call, t(lang, "adm_promo_choose_once"), b.as_markup())


@router.callback_query(AdminCB.filter((F.s == "promo") & (F.a == "save")))
async def cb_promo_save(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User,
                        state: FSMContext) -> None:
    wiz = dict((await state.get_data()).get("wiz") or {})
    if not wiz.get("code") or "max_uses" not in wiz:
        await call.answer(t(user.language, "adm_wizard_lost"), show_alert=True)
        return
    if await session.scalar(select(PromoCode.id).where(PromoCode.code == wiz["code"])):
        await call.answer(t(user.language, "adm_promo_exists"), show_alert=True)
        return
    now = utcnow()
    plans = (await session.scalars(select(Plan).where(Plan.id.in_(wiz.get("plans") or [])))).all()
    promo = PromoCode(
        code=wiz["code"], kind=wiz.get("kind", "percent"), value=int(wiz.get("value", 0)),
        currency=wiz.get("currency"), bonus_days=int(wiz.get("bonus_days", 0)),
        valid_until=now + timedelta(days=wiz["valid_days"]) if wiz.get("valid_days") else None,
        max_uses=wiz["max_uses"] or None, once_per_user=callback_data.x == "1", is_active=True,
        created_by=user.id, created_at=now, plans=list(plans),
    )
    session.add(promo)
    await audit(session, user.id, "promo_create", f"promo:{promo.code}", promo_summary("en", promo))
    await session.commit()
    await state.clear()
    text, kb = promo_card(user.language, promo)
    await edit(call, t(user.language, "adm_promo_created") + "\n\n" + text, kb)
