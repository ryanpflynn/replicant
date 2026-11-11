---
paths:
  - "src/**/*.py"
  - "tests/**/*.py"
---
# Python style

- Type-annotate every function signature; prefer `X | None` over `Optional[X]`.
- Money arithmetic uses `Decimal` and `billing.money`; a float in an amount calculation is a bug.
- New behaviour gets a pytest test; use `pytest.mark.parametrize` for several cases of one rule.
