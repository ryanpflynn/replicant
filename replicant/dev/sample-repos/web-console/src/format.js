// Display helpers for the billing console. Amounts are integer cents.

/** Format integer cents as a currency string, e.g. formatCents(123456, "EUR") -> "EUR 1,234.56". */
export function formatCents(cents, currency) {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  const whole = Math.floor(abs / 100).toLocaleString("en-US");
  const frac = String(abs % 100).padStart(2, "0");
  return `${sign}${currency} ${whole}.${frac}`;
}

/** The invoice status badge shown in the list. */
export function statusBadge(invoice) {
  if (invoice.paidCents >= invoice.totalCents) return "paid";
  if (invoice.dueDate < invoice.today) return "overdue";
  return "open";
}
