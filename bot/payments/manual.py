from __future__ import annotations

from bot.db.models import Order, PaymentMethodInfo
from bot.payments.base import PaymentInstructions, PaymentMethod


class ManualPayment(PaymentMethod):
    """Transfer to the owner's card/wallet; the buyer sends a receipt, an admin approves."""

    provider = "manual"
    requires_manual_review = True

    async def create_payment(self, order: Order, info: PaymentMethodInfo) -> PaymentInstructions:
        return PaymentInstructions(text=info.details)
