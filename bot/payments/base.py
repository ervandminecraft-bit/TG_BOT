"""Payment method interface.

Today only the "manual" provider exists (an admin checks the payment by hand).
Automatic providers (YooKassa, Stripe, CryptoBot, Telegram Stars...) can be added
later by implementing PaymentMethod and registering it in registry.py.
The order/subscription core stays the same: a provider only has to move an order
to "approved" through services.orders.approve_order().
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from bot.db.models import Order, PaymentMethodInfo


@dataclass
class PaymentInstructions:
    text: str  # what to show the buyer (requisites, link...)
    pay_url: str | None = None  # for future automatic providers
    extra: dict = field(default_factory=dict)


class PaymentMethod(ABC):
    provider: str = ""
    #: True -> the buyer sends a receipt and an admin approves manually.
    requires_manual_review: bool = True

    @abstractmethod
    async def create_payment(self, order: Order, info: PaymentMethodInfo) -> PaymentInstructions:
        """Prepare payment for an order (e.g. return requisites or a payment link)."""

    async def check_payment(self, order: Order) -> bool | None:
        """Automatic providers return True/False; manual returns None (admin decides)."""
        return None
