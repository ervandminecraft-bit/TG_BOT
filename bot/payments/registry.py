from __future__ import annotations

from bot.payments.base import PaymentMethod
from bot.payments.manual import ManualPayment

PROVIDERS: dict[str, PaymentMethod] = {
    "manual": ManualPayment(),
}


def get_provider(name: str) -> PaymentMethod:
    return PROVIDERS.get(name) or PROVIDERS["manual"]
