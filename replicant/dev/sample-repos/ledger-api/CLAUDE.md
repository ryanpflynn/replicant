# ledger-api conventions

- Money is integer minor units (`int64`); never use floating point for amounts.
- Rounding is half-even, through the helpers in `internal/ledger` (`Prorate`).
- Tests are table-driven (`t.Run` per case) and live next to the code.
- Run `go vet ./... && go test ./...` before finishing.
