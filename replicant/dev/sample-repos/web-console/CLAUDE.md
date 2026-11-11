# web-console conventions

- Amounts are integer cents; format them only through `src/format.js`.
- Tests use vitest (`test/*.test.js`), table-driven with `it.each` where there are several cases.
- `pnpm test` needs the libvips CLI (`vips`), as in CI.
