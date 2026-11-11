# billing-py conventions

- Money is integer cents (`int`); ratios are `Decimal`. Never use floats for amounts.
- Rounding is half-even, through `billing.money` (`round_cents`, `percent_of`).
- Tests use pytest, live under `tests/`, and are parametrized where there are several cases.
- Run `uv run pytest -q && uv run ruff check .` before finishing.
