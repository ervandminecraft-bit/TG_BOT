"""Admin home, orders queue/history, stats, export, audit log, backup."""
from __future__ import annotations

from aiogram import Bot, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import AuditLog, Order, OrderStatus, User
from bot.handlers.admin.common import Ctx, admin_router, ask, edit, on_input, retry
from bot.i18n import t
from bot.keyboards import AdminCB, MenuCB, admin_back_kb
from bot.services import stats as stats_svc
from bot.services.backup import make_backup
from bot.services.common import audit
from bot.services.notifications import send_admin_cards, status_label
from bot.utils.money import fmt_amount
from bot.utils.text import esc
from bot.utils.timeutil import fmt_dt, utcnow

router = admin_router("admin_home")
PAGE = 10


async def pending_count(session: AsyncSession) -> int:
    return int(await session.scalar(
        select(func.count()).select_from(Order).where(Order.status == OrderStatus.PENDING_REVIEW)) or 0)


async def home_kb(session: AsyncSession, lang: str):
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "adm_btn_orders", n=await pending_count(session)), callback_data=AdminCB(s="orders"))
    b.button(text=t(lang, "adm_btn_stats"), callback_data=AdminCB(s="stats"))
    b.button(text=t(lang, "adm_btn_users"), callback_data=AdminCB(s="users"))
    b.button(text=t(lang, "adm_btn_channels"), callback_data=AdminCB(s="ch"))
    b.button(text=t(lang, "adm_btn_plans"), callback_data=AdminCB(s="pl"))
    b.button(text=t(lang, "adm_btn_methods"), callback_data=AdminCB(s="pm"))
    b.button(text=t(lang, "adm_btn_promo"), callback_data=AdminCB(s="promo"))
    b.button(text=t(lang, "adm_btn_broadcast"), callback_data=AdminCB(s="bc"))
    b.button(text=t(lang, "adm_btn_export"), callback_data=AdminCB(s="export"))
    b.button(text=t(lang, "adm_btn_audit"), callback_data=AdminCB(s="audit"))
    b.button(text=t(lang, "adm_btn_backup"), callback_data=AdminCB(s="backup"))
    b.button(text=t(lang, "btn_main_menu"), callback_data=MenuCB(action="main"))
    b.adjust(1, 2, 2, 2, 2, 2, 1)
    return b.as_markup()


@router.message(Command("admin"))
async def cmd_admin(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.clear()
    await message.answer(t(user.language, "adm_home"), reply_markup=await home_kb(session, user.language))


@router.callback_query(AdminCB.filter(F.s == "home"))
async def cb_home(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    await edit(call, t(user.language, "adm_home"), await home_kb(session, user.language))


# ------------------------------------------------------------------ orders
@router.callback_query(AdminCB.filter((F.s == "orders") & (F.a == "")))
async def cb_orders(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    lang = user.language
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "adm_btn_queue", n=await pending_count(session)), callback_data=AdminCB(s="orders", a="queue"))
    for status in (OrderStatus.APPROVED, OrderStatus.REJECTED, OrderStatus.AWAITING_PAYMENT,
                   OrderStatus.EXPIRED, OrderStatus.CANCELLED):
        b.button(text=status_label(lang, status), callback_data=AdminCB(s="orders", a="list", x=status))
    b.button(text=t(lang, "adm_btn_find_order"), callback_data=AdminCB(s="orders", a="find"))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="home"))
    b.adjust(1, 2, 2, 1, 1, 1)
    await edit(call, t(lang, "adm_orders_title"), b.as_markup())


def _order_line(order: Order, user: User | None) -> str:
    who = f"@{user.username}" if user and user.username else str(order.user_id)
    return f"#{order.code} · {fmt_amount(order.final_amount, order.currency)} · {who}"


async def _orders_list(call: CallbackQuery, session: AsyncSession, lang: str, status: str, page: int) -> None:
    q = select(Order, User).join(User, User.id == Order.user_id).where(Order.status == status)
    order_by = Order.proof_submitted_at if status == OrderStatus.PENDING_REVIEW else Order.updated_at.desc()
    total = int(await session.scalar(select(func.count()).select_from(Order).where(Order.status == status)) or 0)
    rows = (await session.execute(q.order_by(order_by).offset(page * PAGE).limit(PAGE))).all()
    b = InlineKeyboardBuilder()
    for order, buyer in rows:
        b.button(text=_order_line(order, buyer)[:60], callback_data=AdminCB(s="orders", a="card", i=order.id))
    nav = 0
    if page > 0:
        b.button(text="⬅️", callback_data=AdminCB(s="orders", a="list", i=page - 1, x=status))
        nav += 1
    if (page + 1) * PAGE < total:
        b.button(text="➡️", callback_data=AdminCB(s="orders", a="list", i=page + 1, x=status))
        nav += 1
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="orders"))
    b.adjust(*([1] * len(rows)), *([nav] if nav else []), 1)
    text = t(lang, "adm_orders_list", status=status_label(lang, status), total=total)
    if not rows:
        text += "\n\n" + t(lang, "adm_empty")
    await edit(call, text, b.as_markup())


@router.callback_query(AdminCB.filter((F.s == "orders") & (F.a == "queue")))
async def cb_queue(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    await _orders_list(call, session, user.language, OrderStatus.PENDING_REVIEW, 0)


@router.callback_query(AdminCB.filter((F.s == "orders") & (F.a == "list")))
async def cb_list(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    status = callback_data.x if callback_data.x in OrderStatus.ALL else OrderStatus.PENDING_REVIEW
    await _orders_list(call, session, user.language, status, callback_data.i)


@router.callback_query(AdminCB.filter((F.s == "orders") & (F.a == "card")))
async def cb_order_card(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User, bot: Bot) -> None:
    order = await session.get(Order, callback_data.i)
    if order is None:
        await call.answer(t(user.language, "adm_not_found"), show_alert=True)
        return
    await call.answer()
    await send_admin_cards(bot, session, order, only_admin=user.id)


@router.callback_query(AdminCB.filter((F.s == "orders") & (F.a == "find")))
async def cb_find(call: CallbackQuery, user: User, state: FSMContext) -> None:
    await ask(call, state, user.language, "find_order", "adm_enter_order_code", back=("orders", "", 0))


@on_input("find_order")
async def in_find_order(ctx: Ctx) -> None:
    code = ctx.text.lstrip("#").upper()
    order = await ctx.session.scalar(select(Order).where(Order.code == code))
    if order is None:
        await retry(ctx, "adm_not_found")
        return
    await ctx.state.clear()
    await send_admin_cards(ctx.bot, ctx.session, order, only_admin=ctx.user.id)


# ------------------------------------------------------------------ stats
@router.callback_query(AdminCB.filter(F.s == "stats"))
async def cb_stats(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    lang = user.language
    now = utcnow()
    parts = [t(lang, "stats_title"), t(lang, "stats_active_total", n=await stats_svc.total_active(session, now))]
    for title, cnt in await stats_svc.active_by_channel(session, now):
        parts.append(t(lang, "stats_channel_line", title=esc(title), n=cnt))
    for period in stats_svc.periods(now):
        parts.append("\n" + t(lang, f"stats_period_{period.key}"))
        rev = await stats_svc.revenue(session, period.start)
        if rev:
            for method, currency, total, count in rev:
                parts.append(t(lang, "stats_revenue_line", method=esc(method), amount=fmt_amount(total, currency), n=count))
        else:
            parts.append(t(lang, "stats_no_revenue"))
        new, renewed = await stats_svc.new_and_renewed(session, period.start)
        parts.append(t(lang, "stats_flow", new=new, renewed=renewed,
                       churned=await stats_svc.churned(session, period.start, now)))
    promos = await stats_svc.promo_stats(session)
    if promos:
        parts.append("\n" + t(lang, "stats_promo_title"))
        for code, used, money in promos:
            parts.append(t(lang, "stats_promo_line", code=esc(code), used=used, revenue=stats_svc.money_list(money)))
    referred, rewards, money = await stats_svc.referral_stats(session)
    parts.append("\n" + t(lang, "stats_ref", referred=referred, rewards=rewards, revenue=stats_svc.money_list(money)))
    await edit(call, "\n".join(parts), admin_back_kb(lang))


# ------------------------------------------------------------------ export
@router.callback_query(AdminCB.filter((F.s == "export") & (F.a == "")))
async def cb_export(call: CallbackQuery, user: User) -> None:
    lang = user.language
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "adm_btn_export_orders"), callback_data=AdminCB(s="export", a="orders"))
    b.button(text=t(lang, "adm_btn_export_subs"), callback_data=AdminCB(s="export", a="subs"))
    b.button(text=t(lang, "btn_back"), callback_data=AdminCB(s="home"))
    b.adjust(1)
    await edit(call, t(lang, "adm_export_title"), b.as_markup())


@router.callback_query(AdminCB.filter((F.s == "export") & (F.a.in_({"orders", "subs"}))))
async def cb_export_file(call: CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: User) -> None:
    await call.answer()
    stamp = utcnow().strftime("%Y%m%d-%H%M")
    if callback_data.a == "orders":
        data, name = await stats_svc.export_orders(session), f"orders-{stamp}.csv"
    else:
        data, name = await stats_svc.export_subscribers(session, utcnow()), f"subscribers-{stamp}.csv"
    await audit(session, user.id, "export", callback_data.a)
    await session.commit()
    await call.message.answer_document(BufferedInputFile(data, filename=name), caption=t(user.language, "adm_export_done"))


# ------------------------------------------------------------------ audit log
@router.callback_query(AdminCB.filter(F.s == "audit"))
async def cb_audit(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    lang = user.language
    rows = (await session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(30))).all()
    lines = [t(lang, "audit_title")]
    for row in rows:
        lines.append(f"{fmt_dt(row.created_at, settings.tz)} · <code>{row.admin_id or 'system'}</code> · "
                     f"{esc(row.action)} · {esc(row.target or '')} {esc((row.details or '')[:80])}")
    if not rows:
        lines.append(t(lang, "adm_empty"))
    await edit(call, "\n".join(lines)[:4000], admin_back_kb(lang))


# ------------------------------------------------------------------ backup
@router.callback_query(AdminCB.filter(F.s == "backup"))
async def cb_backup(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    await call.answer(t(user.language, "backup_started"))
    path = await make_backup()
    await audit(session, user.id, "backup", path.name)
    await session.commit()
    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb < 49:
        await call.message.answer_document(FSInputFile(path), caption=t(user.language, "backup_done", name=path.name))
    else:
        await call.message.answer(t(user.language, "backup_too_big", name=path.name))

