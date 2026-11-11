"""An invoice: lines, an optional percentage discount and a tax rate."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from billing.money import percent_of, round_cents


@dataclass(frozen=True)
class Line:
    description: str
    unit_cents: int
    quantity: int = 1

    @property
    def total_cents(self) -> int:
        return self.unit_cents * self.quantity


@dataclass
class Invoice:
    lines: list[Line] = field(default_factory=list)
    discount_percent: Decimal = Decimal(0)
    tax_rate: Decimal = Decimal(0)  # e.g. Decimal("0.2") for 20 %

    def subtotal_cents(self) -> int:
        return sum(line.total_cents for line in self.lines)

    def discount_cents(self) -> int:
        return percent_of(self.subtotal_cents(), self.discount_percent)

    def taxable_cents(self) -> int:
        return self.subtotal_cents() - self.discount_cents()

    def tax_cents(self) -> int:
        return round_cents(Decimal(self.subtotal_cents()) * self.tax_rate)

    def total_cents(self) -> int:
        return self.taxable_cents() + self.tax_cents()
