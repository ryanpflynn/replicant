package ledger

// Prorate returns amount * part / whole, rounded half-even to a whole minor unit.
// It uses integer arithmetic only. whole must be > 0 and 0 <= part <= whole.
func Prorate(amount, part, whole int64) int64 {
	if whole <= 0 {
		panic("ledger.Prorate: whole must be > 0")
	}
	num := amount * part
	q, r := num/whole, num%whole
	switch twice := 2 * r; {
	case twice > whole:
		q++
	case twice == whole && q%2 != 0:
		q++
	}
	return q
}
