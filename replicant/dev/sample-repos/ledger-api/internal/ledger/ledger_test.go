package ledger

import "testing"

func TestApplyCharge(t *testing.T) {
	got := ApplyCharge(Charge{ID: "ch_1", Currency: "EUR", AmountMinor: 10000, FXFeeMinor: 48})
	if len(got) != 2 || got[0].AmountMinor != 10000 || got[1].Kind != KindFXFee || got[1].AmountMinor != 48 {
		t.Fatalf("ApplyCharge = %v", got)
	}
}

func TestApplyRefund(t *testing.T) {
	charge := Charge{ID: "ch_1", Currency: "EUR", AmountMinor: 10000, FXFeeMinor: 48}
	cases := []struct {
		name       string
		refund     int64
		wantRefund int64
		wantFX     int64
	}{
		{name: "full_eur_fx_fee", refund: 10000, wantRefund: -10000, wantFX: -48},
		{name: "partial_eur_fx_fee", refund: 4000, wantRefund: -4000, wantFX: -19},
		{name: "partial_half_even", refund: 2500, wantRefund: -2500, wantFX: -12},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ApplyRefund(charge, tc.refund)
			if err != nil {
				t.Fatal(err)
			}
			if len(got) != 2 {
				t.Fatalf("ApplyRefund returned %d entries, want 2: %v", len(got), got)
			}
			if got[0].Kind != KindRefund || got[0].AmountMinor != tc.wantRefund {
				t.Errorf("refund amount = %s %d, want %s %d", got[0].Currency, got[0].AmountMinor, charge.Currency, tc.wantRefund)
			}
			if got[1].Kind != KindFXFeeReversal || got[1].AmountMinor != tc.wantFX {
				t.Errorf("fx_fee_reversal amount = %s %d, want %s %d", got[1].Currency, got[1].AmountMinor, charge.Currency, tc.wantFX)
			}
		})
	}
}

func TestApplyRefundRejectsInvalidAmounts(t *testing.T) {
	charge := Charge{ID: "ch_1", Currency: "EUR", AmountMinor: 10000}
	for _, amt := range []int64{0, -1, 10001} {
		if _, err := ApplyRefund(charge, amt); err != ErrInvalidRefund {
			t.Errorf("ApplyRefund(%d) err = %v, want ErrInvalidRefund", amt, err)
		}
	}
}

func TestProrate(t *testing.T) {
	cases := []struct{ amount, part, whole, want int64 }{
		{48, 4000, 10000, 19}, // 19.2
		{48, 2500, 10000, 12}, // 12.0
		{50, 2500, 10000, 12}, // 12.5 -> 12 (half-even)
		{54, 2500, 10000, 14}, // 13.5 -> 14 (half-even)
		{48, 10000, 10000, 48},
		{48, 0, 10000, 0},
	}
	for _, c := range cases {
		if got := Prorate(c.amount, c.part, c.whole); got != c.want {
			t.Errorf("Prorate(%d, %d, %d) = %d, want %d", c.amount, c.part, c.whole, got, c.want)
		}
	}
}
