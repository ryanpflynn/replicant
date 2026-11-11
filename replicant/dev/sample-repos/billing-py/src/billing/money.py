"""Money helpers: amounts are integer cents, ratios Decimal, rounding half-even to the cent."""

from decimal import ROUND_HALF_EVEN, Decimal


def round_cents(value: Decimal) -> int:
    """Round a Decimal amount of cents to whole cents, half-even."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def percent_of(amount_cents: int, percent: Decimal | int) -> int:
    """`percent`% of an amount, rounded half-even to the cent."""
    return round_cents(Decimal(amount_cents) * Decimal(percent) / Decimal(100))
