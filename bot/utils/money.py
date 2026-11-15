"""Money is stored in minor units (1/100) as integers to avoid float errors."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_AMOUNT = 100_000_000_00  # sanity limit


def parse_amount(text: str) -> int | None:
    """'1490' -> 149000, '9.99' / '9,99' -> 999. Returns None if invalid."""
    cleaned = text.strip().replace(" ", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    minor = int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if minor > MAX_AMOUNT:
        return None
    return minor


def fmt_amount(minor: int, currency: str | None = None) -> str:
    whole, frac = divmod(abs(minor), 100)
    sign = "-" if minor < 0 else ""
    text = f"{whole:,}".replace(",", " ")
    if frac:
        text += f".{frac:02d}"
    text = sign + text
    return f"{text} {currency}" if currency else text
