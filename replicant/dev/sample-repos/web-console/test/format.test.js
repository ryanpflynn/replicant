import { describe, expect, it } from "vitest";
import { formatCents, statusBadge } from "../src/format.js";

describe("formatCents", () => {
  it.each([
    [0, "EUR 0.00"],
    [5, "EUR 0.05"],
    [123456, "EUR 1,234.56"],
    [-1999, "-EUR 19.99"],
  ])("formats %i", (cents, want) => {
    expect(formatCents(cents, "EUR")).toBe(want);
  });
});

describe("statusBadge", () => {
  it("is paid when fully paid", () => {
    expect(statusBadge({ paidCents: 100, totalCents: 100, dueDate: "2026-01-01", today: "2026-02-01" })).toBe("paid");
  });
  it("is overdue after the due date", () => {
    expect(statusBadge({ paidCents: 0, totalCents: 100, dueDate: "2026-01-01", today: "2026-02-01" })).toBe("overdue");
  });
  it("is open before the due date", () => {
    expect(statusBadge({ paidCents: 0, totalCents: 100, dueDate: "2026-03-01", today: "2026-02-01" })).toBe("open");
  });
});
