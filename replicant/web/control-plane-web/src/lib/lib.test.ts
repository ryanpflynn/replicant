import { describe, expect, it } from "vitest";
import { age, duration, humanize, timeLeft, usd } from "./format";
import { jobStatus, runStatus, TERMINAL } from "./status";
import { splitId, ulidBody } from "./ids";
import { parsePatch, untar, wordDiff } from "./diff";
import { browserGitUrl, repoName } from "./config";

describe("format", () => {
  it("durations and ages", () => {
    expect(duration(8_000)).toBe("8 s");
    expect(duration(98)).toBe("98 ms");
    expect(duration(843_000)).toBe("14m 03s");
    expect(duration(3_720_000)).toBe("1h 02m");
    expect(age(new Date(Date.now() - 3 * 3600_000).toISOString())).toBe("3 h");
    expect(timeLeft(new Date(Date.now() + 5.5 * 3600_000).toISOString())).toBe("5h left");
    expect(timeLeft(new Date(Date.now() - 1000).toISOString())).toBe("expired");
    expect(usd(2.857)).toBe("$2.86");
    expect(humanize("publish_approval")).toBe("Publish approval");
  });
});

describe("status vocabulary (tokens.json#status)", () => {
  it("maps job and run states to shape + family", () => {
    expect(jobStatus("waiting_human")).toMatchObject({ family: "attention", glyph: "diamond" });
    expect(jobStatus("waiting_external").label).toBe("In PR review");
    expect(jobStatus("succeeded")).toMatchObject({ family: "success", glyph: "check" });
    expect(runStatus("Running").glyph).toBe("ring-spin");
    expect(TERMINAL.has("taken_over")).toBe(true);
    expect(TERMINAL.has("paused")).toBe(false);
  });
});

describe("ids", () => {
  it("generates C-IDS-shaped bodies and splits for display", () => {
    const b = ulidBody(1_761_374_400_000, () => new Uint8Array(16).fill(7));
    expect(b).toMatch(/^[0-9a-hjkmnp-tv-z]{26}$/);
    expect(splitId("job_01m5rwhr8j6c9rj9crvm6jp5fm")).toEqual({ pfx: "job_", head: "01m5rwhr8j", rest: "6c9rj9crvm6jp5fm" });
  });
});

describe("diff", () => {
  const patch = [
    "diff --git a/internal/ledger/ledger.go b/internal/ledger/ledger.go",
    "index 1111111..2222222 100644",
    "--- a/internal/ledger/ledger.go",
    "+++ b/internal/ledger/ledger.go",
    "@@ -40,3 +40,3 @@ func ApplyRefund(c Charge, refundMinor int64) []Entry {",
    " \tentries := []Entry{}",
    "-\tfee := c.FXFeeMinor",
    "+\tfee := Prorate(c.FXFeeMinor, refundMinor, c.AmountMinor)",
    " \treturn entries",
    "",
  ].join("\n");
  it("parses files, hunks and line numbers", () => {
    const [f] = parsePatch(patch);
    expect(f.path).toBe("internal/ledger/ledger.go");
    expect(f.adds).toBe(1);
    expect(f.dels).toBe(1);
    expect(f.lines[0].kind).toBe("hunk");
    expect(f.lines[2]).toMatchObject({ kind: "del", oldNo: 41 });
    expect(f.lines[3]).toMatchObject({ kind: "add", newNo: 41 });
  });
  it("highlights the changed words only", () => {
    const [a, b] = wordDiff("\tfee := c.FXFeeMinor", "\tfee := Prorate(c.FXFeeMinor, refundMinor, c.AmountMinor)");
    expect(a.find((w) => w.changed)?.text).toBe("c.FXFeeMinor");
    expect(b.find((w) => w.changed)?.text).toBe("Prorate(c.FXFeeMinor, refundMinor, c.AmountMinor)");
  });
  it("reads a ustar archive", () => {
    const data = new TextEncoder().encode("hello");
    const hdr = new Uint8Array(512);
    hdr.set(new TextEncoder().encode("changes.patch"), 0);
    hdr.set(new TextEncoder().encode("00000000005\0"), 124);
    hdr[156] = 48;
    const buf = new Uint8Array(512 * 4);
    buf.set(hdr, 0);
    buf.set(data, 512);
    const [e] = untar(buf);
    expect(e.name).toBe("changes.patch");
    expect(new TextDecoder().decode(e.data)).toBe("hello");
  });
});

describe("links", () => {
  it("maps in-network Gitea hosts to the browser address", () => {
    expect(browserGitUrl("http://gitea:3300/acme-payments/ledger-api/pulls/2")).toBe("http://127.0.0.1:3300/acme-payments/ledger-api/pulls/2");
    expect(repoName("git.replicant.localhost:3300/acme-payments/ledger-api")).toBe("acme-payments/ledger-api");
  });
});
