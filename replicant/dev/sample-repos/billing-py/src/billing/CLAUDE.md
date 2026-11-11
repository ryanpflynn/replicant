# src/billing

- `money.py` owns rounding: call `round_cents` or `percent_of`; never round with `round()` or floats.
- Tax applies to the taxable amount (after discounts), never to the gross subtotal.
- Keep `Invoice` methods pure; no I/O in this package.
