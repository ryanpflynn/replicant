from decimal import Decimal

import pytest

from billing import Invoice, Line
from billing.money import percent_of, round_cents


@pytest.mark.parametrize(
    "value,want",
    [(Decimal("2.5"), 2), (Decimal("3.5"), 4), (Decimal("-2.5"), -2), (Decimal("10.49"), 10)],
)
def test_round_cents_half_even(value: Decimal, want: int) -> None:
    assert round_cents(value) == want


def test_percent_of() -> None:
    assert percent_of(1999, 10) == 200
    assert percent_of(1000, Decimal("12.5")) == 125


def test_subtotal_and_total_without_discount() -> None:
    inv = Invoice([Line("seat", 1200, 3), Line("support", 4900)], tax_rate=Decimal("0.2"))
    assert inv.subtotal_cents() == 8500
    assert inv.discount_cents() == 0
    assert inv.tax_cents() == 1700
    assert inv.total_cents() == 10200


def test_discount_without_tax() -> None:
    inv = Invoice([Line("seat", 1000, 2)], discount_percent=Decimal(25))
    assert inv.discount_cents() == 500
    assert inv.total_cents() == 1500
