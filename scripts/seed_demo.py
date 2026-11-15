"""Add made-up demo payment methods and plans to the bot (safe to run while the bot is running).

    python -m scripts.seed_demo            add demo data (items that already exist are skipped)
    python -m scripts.seed_demo --remove   remove demo data again

All requisites below are FAKE. Replace them with real ones in the admin panel
(🛠 Admin panel → Payment methods) before real customers pay.
Demo plans are linked to every active channel, so they appear in the catalog right away.
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select

from bot.config import settings
from bot.db.migrations import run_migrations
from bot.db.models import Channel, Order, PaymentMethodInfo, Plan, PlanPrice
from bot.db.session import make_engine, make_session_maker

# name, currency, requisites (fake)
METHODS = [
    ("💳 Карта РФ (демо)", "RUB",
     "Номер карты: 2200 0000 0000 0000\nПолучатель: Иван И.\nБанк: Демо-банк\n"
     "⚠️ Это демо-реквизиты, деньги сюда не переводите."),
    ("📱 СБП по номеру (демо)", "RUB",
     "Телефон: +7 900 000-00-00\nБанк получателя: Демо-банк\nПолучатель: Иван И.\n"
     "⚠️ Это демо-реквизиты, деньги сюда не переводите."),
    ("🌍 Карта Visa/Mastercard (демо)", "USD",
     "Номер карты: 4000 0000 0000 0000\nCardholder: IVAN I.\n"
     "⚠️ Это демо-реквизиты, деньги сюда не переводите."),
    ("💲 USDT TRC-20 (демо)", "USDT",
     "Сеть: TRON (TRC-20)\nАдрес: TDEMOxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
     "Отправляйте только USDT в сети TRC-20.\n⚠️ Это демо-адрес, деньги сюда не переводите."),
]
# Demo methods from earlier versions: no longer added, but still cleaned up by --remove.
LEGACY_METHOD_NAMES = ["💎 TON (демо)"]

# name, description, days (None = lifetime), prices in major units per method index
PLANS = [
    ("🗓 Неделя — пробная", "Попробуйте канал 7 дней по минимальной цене.", 7,
     [1000, 1000, 2.5, 2.5]),
    ("📅 Месяц", "Самый популярный тариф: 30 дней доступа.", 30,
     [2500, 2500, 7, 7]),
    ("📆 3 месяца — выгода 15%", "90 дней доступа, дешевле помесячной оплаты.", 90,
     [6000, 6000, 18, 18]),
    ("🏆 Год — выгода 35%", "365 дней доступа по лучшей цене за месяц.", 365,
     [12000, 12000, 55, 55]),
    ("♾ Навсегда", "Разовая оплата, доступ без срока.", None,
     [15000, 15000, 120, 120]),
]


async def add(session) -> None:
    channels = (await session.scalars(select(Channel).where(Channel.is_active.is_(True)))).all()
    if not channels:
        print("⚠️ Нет активных каналов: тарифы создадутся, но в каталоге не появятся, пока к ним не привязан канал.")

    base_order = (await session.scalar(select(func.max(PaymentMethodInfo.sort_order)))) or 0
    methods: list[PaymentMethodInfo] = []
    for i, (name, currency, details) in enumerate(METHODS, start=1):
        m = await session.scalar(select(PaymentMethodInfo).where(PaymentMethodInfo.name == name))
        if m is None:
            m = PaymentMethodInfo(name=name, currency=currency, details=details, sort_order=base_order + i)
            session.add(m)
            print(f"+ способ оплаты: {name} ({currency})")
        else:
            print(f"= уже есть: {name}")
        methods.append(m)
    await session.flush()

    base_order = (await session.scalar(select(func.max(Plan.sort_order)))) or 0
    for i, (name, desc, days, prices) in enumerate(PLANS, start=1):
        if await session.scalar(select(Plan).where(Plan.name == name)) is not None:
            print(f"= уже есть: {name}")
            continue
        plan = Plan(name=name, description=desc, duration_days=days, sort_order=base_order + i)
        plan.channels = list(channels)
        session.add(plan)
        await session.flush()
        for method, amount in zip(methods, prices):
            session.add(PlanPrice(plan_id=plan.id, payment_method_id=method.id, amount=round(amount * 100)))
        print(f"+ тариф: {name}")
    await session.commit()


async def remove(session) -> None:
    for name, *_ in PLANS:
        plan = await session.scalar(select(Plan).where(Plan.name == name))
        if plan is None:
            continue
        if await session.scalar(select(func.count()).select_from(Order).where(Order.plan_id == plan.id)):
            plan.is_active = False  # has orders: keep history, just hide it
            print(f"~ скрыт (есть заказы): {name}")
        else:
            await session.delete(plan)
            print(f"- удалён тариф: {name}")
    for name in [m[0] for m in METHODS] + LEGACY_METHOD_NAMES:
        m = await session.scalar(select(PaymentMethodInfo).where(PaymentMethodInfo.name == name))
        if m is None:
            continue
        if await session.scalar(select(func.count()).select_from(Order).where(Order.payment_method_id == m.id)):
            m.is_active = False
            print(f"~ выключен (есть заказы): {name}")
        else:
            await session.delete(m)
            print(f"- удалён способ оплаты: {name}")
    await session.commit()


async def main() -> None:
    engine = make_engine(settings.db_file)
    await run_migrations(engine)
    sm = make_session_maker(engine)
    async with sm() as session:
        await (remove(session) if "--remove" in sys.argv else add(session))
    await engine.dispose()
    print("Готово.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
