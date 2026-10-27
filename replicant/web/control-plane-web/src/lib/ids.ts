// C-IDS helpers for the browser: Crockford base32 (lowercase, no i l o u) ULID bodies for action_id / message_id /
// Idempotency-Key, and the display split used by the Id component (prefix dimmed, 10 chars ink, rest dimmed).
const ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz";

export function ulidBody(now = Date.now(), rand: (n: number) => Uint8Array = randomBytes): string {
  let t = now;
  let time = "";
  for (let i = 0; i < 10; i++) { time = ALPHABET[t % 32] + time; t = Math.floor(t / 32); }
  const r = rand(16);
  let tail = "";
  for (let i = 0; i < 16; i++) tail += ALPHABET[r[i] % 32];
  return time + tail;
}

function randomBytes(n: number): Uint8Array {
  const b = new Uint8Array(n);
  crypto.getRandomValues(b);
  return b;
}

export const newId = (prefix: string) => `${prefix}_${ulidBody()}`;

export interface IdParts { pfx: string; head: string; rest: string }

export function splitId(id: string, headLen = 10): IdParts {
  const m = /^([a-z]+_)(.*)$/.exec(id);
  if (!m) return { pfx: "", head: id, rest: "" };
  return { pfx: m[1], head: m[2].slice(0, headLen), rest: m[2].slice(headLen) };
}

export const shortId = (id: string, headLen = 10) => { const p = splitId(id, headLen); return p.pfx + p.head; };
