// Package ledger posts double-entry ledger lines for card charges and refunds.
//
// Amounts are int64 minor units (cents for EUR/USD). Never use float64 for money.
package ledger

import (
	"errors"
	"fmt"
)

// Kind is the type of a ledger entry.
type Kind string

const (
	KindCharge        Kind = "charge"
	KindFXFee         Kind = "fx_fee"
	KindRefund        Kind = "refund"
	KindFXFeeReversal Kind = "fx_fee_reversal"
)

// Charge is a captured card payment. FXFeeMinor is the foreign-exchange fee that was
// charged on top of AmountMinor when the card currency differed from the merchant currency.
type Charge struct {
	ID          string
	Currency    string
	AmountMinor int64
	FXFeeMinor  int64
}

// Entry is one ledger line. Credits to the customer are negative.
type Entry struct {
	ChargeID    string
	Kind        Kind
	Currency    string
	AmountMinor int64
}

func (e Entry) String() string {
	return fmt.Sprintf("%s %s %d", e.Kind, e.Currency, e.AmountMinor)
}

var (
	ErrInvalidRefund = errors.New("ledger: refund amount must be > 0 and <= the charge amount")
)

// ApplyCharge returns the entries for a captured charge.
func ApplyCharge(c Charge) []Entry {
	out := []Entry{{ChargeID: c.ID, Kind: KindCharge, Currency: c.Currency, AmountMinor: c.AmountMinor}}
	if c.FXFeeMinor > 0 {
		out = append(out, Entry{ChargeID: c.ID, Kind: KindFXFee, Currency: c.Currency, AmountMinor: c.FXFeeMinor})
	}
	return out
}

// ApplyRefund returns the entries for refunding refundMinor of charge c. A refund reverses the
// refunded amount and the FX fee attributable to it: the whole fee for a full refund, and the
// prorated share for a partial refund.
func ApplyRefund(c Charge, refundMinor int64) ([]Entry, error) {
	if refundMinor <= 0 || refundMinor > c.AmountMinor {
		return nil, ErrInvalidRefund
	}
	out := []Entry{{ChargeID: c.ID, Kind: KindRefund, Currency: c.Currency, AmountMinor: -refundMinor}}
	if c.FXFeeMinor > 0 {
		fxReversal := c.FXFeeMinor
		out = append(out, Entry{ChargeID: c.ID, Kind: KindFXFeeReversal, Currency: c.Currency, AmountMinor: -fxReversal})
	}
	return out, nil
}
