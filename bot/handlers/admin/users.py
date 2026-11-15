"""Admin: subscriber search, user card, manual grant / revoke / refund, resend links."""
from __future__ import annotations

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Order, OrderStatus, Plan, SubStatus, Subscription, User
from bot.handlers.admin.common import Ctx, admin_router, answer_pair, ask, edit, on_input, retry
from bot.i18n import t
from bot.keyboards import AdminCB
from bot.services import access as access_svc
from bot.services.common import audit, safe_send
from bot.services.notifications import plan_duration, status_label
from bot.services.subscriptions import grant, is_sub_active, user_subscriptions
from bot.utils.money import fmt_amount
from bot.utils.text import esc, user_display
from bot.utils.timeutil import fmt_dt, utcnow

router = admin_router("admin_users")


@router.callback_query(AdminCB.filter((F.s == "users") & (F.a == "")))
async def cb_users(call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext) -> None:
    total = await session.scalar(select(func.count()).select_from(User))
    await ask(call, state, user.language, "user_search", "adm_users_prompt", back=("home", "", 0), total=total or 0)


async def find_users(session: AsyncSession, query: str) -> list[User]:
    query = query.strip()
    if query.lstrip("-").isdigit():
        found = await session.get(User, int(query))
        return [found] if found else []
    name = query.lstrip("@").lower()
    if not name:
        return []
    rows = await session.scalars(
        select(User).where(func.lower(User.username).like(f"%{name}%")).order_by(User.username).limit(10)
    )
    return list(rows)


@on_input("user_search")
async def in_user_search(ctx: Ctx) -> None:
    users = await find_users(ctx.session, ctx.text)
    if not users:
        await retry(ctx, "adm_user_not_found")
        return
    await ctx.state.clear()
    if len(users) == 1:
        await answer_pair(ctx.message, await user_card(ctx.session, ctx.lang, users[0]))
        return
    b = InlineKeyboardBuilder()
    for u in users:
        b.button(text=f"@{u.username} · {u.id}"[:60], callback_data=AdminCB(s="users", a="card", i=u.id))
    b.adjust(1)
    await ctx.message.answer(t(ctx.lang, "adm_users_found"), reply_markup=b.as_markup())


async def user_card(session: AsyncSession, lang: str, u: User):
    now = utcnow()
    tz = settings.tz
    lines = [t(lang, "adm_user_card", user=user_display(u.first_name, u.last_name, u.username, u.id),
               language=u.language, created=fmt_dt(u.created_at, tz), seen=fmt_dt(u.last_seen_at, tz),
               blocked=t(lang, "yes") if u.is_blocked else t(lang, "no"),
               referred=u.referred_by or "—", bonus=u.bonus_days_balance)]
    subs = await user_subscriptions(session, u.id)
    lines.append(t(lang, "adm_user_subs"))
    for sub, plan in subs:
        active = is_sub_active(sub, now)
        until = t(lang, "duration_lifetime") if sub.expires_at is None else fmt_dt(sub.expires_at, tz)
        lines.append(f"{'✅' if active else '⛔'} {esc(plan.name)} — {until} ({esc(sub.status)})")
    if not subs:
        lines.append("—")
    orders = (await session.scalars(select(Order).where(Order.user_id == u.id)
                                    .order_by(Order.id.desc()).limit(10))).all()
    lines.append(t(lang, "adm_user_orders"))
    for o in orders:
        refund = " 💸" if o.refunded else ""
        lines.append(f"#{o.code} · {fmt_amount(o.final_amount, o.currency)} · {status_label(lang, o.status)}"
                     f" · {fmt_dt(o.created_at, tz, with_time=False)}{refund}")
    if not orders:
        lines.append("—")
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "adm_btn_grant"), callback_data=AdminCB(s="users", a="grant", i=u.id))
    b.button(text=t(lang, "adm_btn_revoke"), callback_data=AdminCB(s="users", a="revoke", i=u.id))
    b.button(text=t(lang, "adm_btn_refund"), callback_data=AdminCB(s="users", a="refund", i=u.id))
    b.button(text=t(lang, "adm_btn_relink"), callback_data=AdminCB(s="users", a="relink", i=u.id))
    b.button(text=t(lang, "adm_btn_search_again"), callback_data=AdminCB(s="users"))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="home"))
    b.adjust(2, 2, 1, 1)
    return "\n".join(lines)[:4000], b.as_markup()


@router.callback_query(AdminCB.filter((F.s == "users") & (F.a == "card")))
async def cb_user_card(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    target = await session.get(User, callback_data.i)
    if target is None:
        await call.answer(t(user.language, "adm_not_found"), show_alert=True)
        return
    await edit(call, *await user_card(session, user.language, target))


# ------------------------------------------------------------------ grant
@router.callback_query(AdminCB.filter((F.s == "users") & (F.a == "grant")))
async def cb_grant(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    lang = user.language
    plans = (await session.scalars(select(Plan).order_by(Plan.sort_order, Plan.id))).all()
    b = InlineKeyboardBuilder()
    for plan in plans:
        b.button(text=f"{plan.name} · {plan_duration(lang, plan)}"[:60],
                 callback_data=AdminCB(s="users", a="grantp", i=callback_data.i, x=str(plan.id)))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="users", a="card", i=callback_data.i))
    b.adjust(1)
    await edit(call, t(lang, "adm_grant_choose_plan") if plans else t(lang, "adm_empty"), b.as_markup())


@router.callback_query(AdminCB.filter((F.s == "users") & (F.a == "grantp")))
async def cb_grant_plan(call: CallbackQuery, callback_data: AdminCB, user: User, state: FSMContext) -> None:
    await ask(call, state, user.language, "grant_days", "adm_grant_enter_days",
              back=("users", "card", callback_data.i), user_id=callback_data.i, plan_id=int(callback_data.x))


@on_input("grant_days")
async def in_grant_days(ctx: Ctx) -> None:
    if not ctx.text.isdigit() or int(ctx.text) > 36500:
        await retry(ctx, "adm_bad_number")
        return
    days = int(ctx.text) or None  # 0 -> lifetime
    target = await ctx.session.get(User, int(ctx.data["user_id"]))
    plan = await ctx.session.get(Plan, int(ctx.data["plan_id"]))
    if target is None or plan is None:
        await retry(ctx, "adm_not_found")
        return
    now = utcnow()
    sub = await grant(ctx.session, target.id, plan, days, now, source="manual")
    await audit(ctx.session, ctx.user.id, "manual_grant", f"user:{target.id}",
                f"plan={plan.id} days={days or 'lifetime'}")
    await ctx.session.commit()
    await ctx.state.clear()
    until = t(target.language, "duration_lifetime") if sub.expires_at is None else fmt_dt(sub.expires_at, settings.tz)
    await safe_send(ctx.bot, ctx.session, target.id,
                    t(target.language, "manual_grant_user", plan=esc(plan.name), until=until))
    result = await access_svc.deliver_access(ctx.bot, ctx.session, target, now)
    await ctx.session.commit()
    key = "adm_grant_done" if result.ok else "adm_grant_done_link_failed"
    await ctx.message.answer(t(ctx.lang, key, until=fmt_dt(sub.expires_at, settings.tz) if sub.expires_at else
                               t(ctx.lang, "duration_lifetime")))
    await answer_pair(ctx.message, await user_card(ctx.session, ctx.lang, target))


# ------------------------------------------------------------------ revoke
@router.callback_query(AdminCB.filter((F.s == "users") & (F.a == "revoke")))
async def cb_revoke(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    lang = user.language
    now = utcnow()
    subs = [(s, p) for s, p in await user_subscriptions(session, callback_data.i) if is_sub_active(s, now)]
    b = InlineKeyboardBuilder()
    for sub, plan in subs:
        b.button(text=f"⛔ {plan.name}"[:60], callback_data=AdminCB(s="users", a="revokes", i=sub.id))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="users", a="card", i=callback_data.i))
    b.adjust(1)
    await edit(call, t(lang, "adm_revoke_choose") if subs else t(lang, "adm_revoke_none"), b.as_markup())


@router.callback_query(AdminCB.filter((F.s == "users") & (F.a == "revokes")))
async def cb_revoke_confirm(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    lang = user.language
    sub = await session.get(Subscription, callback_data.i)
    if sub is None:
        return
    plan = await session.get(Plan, sub.plan_id)
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "adm_btn_confirm_revoke"), callback_data=AdminCB(s="users", a="revokey", i=sub.id))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="users", a="card", i=sub.user_id))
    b.adjust(1)
    await edit(call, t(lang, "adm_revoke_confirm", plan=esc(plan.name), user_id=sub.user_id), b.as_markup())


@router.callback_query(AdminCB.filter((F.s == "users") & (F.a == "revokey")))
async def cb_revoke_do(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User, bot: Bot) -> None:
    sub = await session.get(Subscription, callback_data.i)
    if sub is None or sub.status != SubStatus.ACTIVE:
        await call.answer(t(user.language, "review_already_done"), show_alert=True)
        return
    now = utcnow()
    sub.status = SubStatus.REVOKED
    sub.removed_at = now
    await session.flush()
    plan = await session.get(Plan, sub.plan_id)
    removed, _failed = await access_svc.remove_uncovered(bot, session, sub.user_id, {c.id for c in plan.channels},
                                                         now)
    await audit(session, user.id, "manual_revoke", f"user:{sub.user_id}", f"plan={plan.id} removed={len(removed)}")
    await session.commit()
    target = await session.get(User, sub.user_id)
    await safe_send(bot, session, target.id, t(target.language, "manual_revoke_user", plan=esc(plan.name)))
    await session.commit()
    await call.answer(t(user.language, "adm_revoked", n=len(removed)), show_alert=True)
    await edit(call, *await user_card(session, user.language, target))


# ------------------------------------------------------------------ refund mark
@router.callback_query(AdminCB.filter((F.s == "users") & (F.a == "refund")))
async def cb_refund(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    lang = user.language
    orders = (await session.scalars(select(Order).where(
        Order.user_id == callback_data.i, Order.status == OrderStatus.APPROVED, Order.refunded.is_(False)
    ).order_by(Order.id.desc()).limit(10))).all()
    b = InlineKeyboardBuilder()
    for o in orders:
        b.button(text=f"💸 #{o.code} · {fmt_amount(o.final_amount, o.currency)}",
                 callback_data=AdminCB(s="users", a="refundo", i=o.id))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="users", a="card", i=callback_data.i))
    b.adjust(1)
    await edit(call, t(lang, "adm_refund_choose") if orders else t(lang, "adm_refund_none"), b.as_markup())


@router.callback_query(AdminCB.filter((F.s == "users") & (F.a == "refundo")))
async def cb_refund_do(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    order = await session.get(Order, callback_data.i)
    if order is None or order.refunded:
        await call.answer(t(user.language, "review_already_done"), show_alert=True)
        return
    order.refunded = True
    await audit(session, user.id, "refund_mark", f"order:{order.code}",
                f"{order.final_amount} {order.currency}")
    await session.commit()
    await call.answer(t(user.language, "adm_refund_done"), show_alert=True)
    target = await session.get(User, order.user_id)
    await edit(call, *await user_card(session, user.language, target))


# ------------------------------------------------------------------ resend links
@router.callback_query(AdminCB.filter((F.s == "users") & (F.a == "relink")))
async def cb_relink(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User, bot: Bot) -> None:
    target = await session.get(User, callback_data.i)
    if target is None:
        return
    result = await access_svc.deliver_access(bot, session, target, header_key="access_relink")
    await audit(session, user.id, "resend_links", f"user:{target.id}")
    await session.commit()
    if not (result.links or result.already or result.approved_pending or result.failed):
        await call.answer(t(user.language, "adm_relink_no_access"), show_alert=True)
    elif result.failed:
        await call.answer(t(user.language, "adm_relink_failed"), show_alert=True)
    else:
        await call.answer(t(user.language, "adm_relink_done"), show_alert=True)

