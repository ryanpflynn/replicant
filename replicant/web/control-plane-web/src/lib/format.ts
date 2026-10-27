// Time, duration and money formatting. Mono timestamps are local time HH:MM:SS (the timeline gutter).

export function parseTs(s?: string | null): number | null {
  if (!s) return null;
  const t = Date.parse(s);
  return Number.isNaN(t) ? null : t;
}

/** Compact duration: 8 s, 14m 03s, 1h 02m, 2d 4h. */
export function duration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

/** Age for list columns: 8 s, 41 m, 3 h, 2 d. */
export function age(iso?: string | null, now = Date.now()): string {
  const t = parseTs(iso);
  if (t == null) return "—";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h`;
  return `${Math.floor(h / 24)} d`;
}

/** Time left until an expiry: "5h left", "2d 4h", "expired". */
export function timeLeft(iso?: string | null, now = Date.now()): string {
  const t = parseTs(iso);
  if (t == null) return "";
  const ms = t - now;
  if (ms <= 0) return "expired";
  const h = Math.floor(ms / 3_600_000);
  if (h < 1) return `${Math.max(1, Math.floor(ms / 60_000))}m left`;
  if (h < 48) return `${h}h left`;
  return `${Math.floor(h / 24)}d ${h % 24}h left`;
}

export const hoursUntil = (iso?: string | null, now = Date.now()) => {
  const t = parseTs(iso);
  return t == null ? Infinity : (t - now) / 3_600_000;
};

export function clock(iso?: string | null): string {
  const t = parseTs(iso);
  if (t == null) return "";
  const d = new Date(t);
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "Oct 25 06:40" (today: "06:40"). */
export function dateTime(iso?: string | null, now = Date.now()): string {
  const t = parseTs(iso);
  if (t == null) return "—";
  const d = new Date(t);
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const n = new Date(now);
  if (d.toDateString() === n.toDateString()) return hm;
  return `${MONTHS[d.getMonth()]} ${d.getDate()} ${hm}`;
}

export const usd = (v?: number | null) => (v == null ? "—" : `$${v.toFixed(2)}`);

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / 1024 / 1024).toFixed(1)} MiB`;
}

export const plural = (n: number, one: string, many = one + "s") => `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;

/** "publish_approval" → "Publish approval". */
export const humanize = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, " ") : s);
