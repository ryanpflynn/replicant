// Diff bundle → unified diff. The bundle is changes.tar.zst (C-ART diff_bundle): zstd-decoded with fzstd, then a
// minimal ustar reader; the patch is parsed into files and hunks for the viewer (components.md §6).
import { decompress } from "fzstd";

export interface TarEntry { name: string; data: Uint8Array }

export function untar(buf: Uint8Array): TarEntry[] {
  const out: TarEntry[] = [];
  const dec = new TextDecoder();
  let off = 0;
  while (off + 512 <= buf.length) {
    const hdr = buf.subarray(off, off + 512);
    if (hdr.every((b) => b === 0)) break;
    const str = (a: number, n: number) => dec.decode(hdr.subarray(a, a + n)).replace(/\0.*$/s, "");
    const prefix = str(345, 155);
    const name = (prefix ? prefix + "/" : "") + str(0, 100);
    const size = parseInt(str(124, 12).trim() || "0", 8);
    const type = String.fromCharCode(hdr[156] || 48);
    off += 512;
    if (type === "0" || type === "\0") out.push({ name, data: buf.slice(off, off + size) });
    off += Math.ceil(size / 512) * 512;
  }
  return out;
}

export function unbundle(zst: Uint8Array): { patch: string; metadata?: Record<string, unknown> } {
  const entries = untar(decompress(zst));
  const dec = new TextDecoder();
  const patchEntry = entries.find((e) => e.name.endsWith(".patch") || e.name.endsWith(".diff"));
  const metaEntry = entries.find((e) => e.name.endsWith("metadata.json"));
  let metadata: Record<string, unknown> | undefined;
  try { metadata = metaEntry ? JSON.parse(dec.decode(metaEntry.data)) : undefined; } catch { metadata = undefined; }
  return { patch: patchEntry ? dec.decode(patchEntry.data) : "", metadata };
}

export type LineKind = "ctx" | "add" | "del" | "hunk" | "meta";
export interface DiffLine { kind: LineKind; text: string; oldNo?: number; newNo?: number }
export interface DiffFile { path: string; oldPath?: string; adds: number; dels: number; lines: DiffLine[]; binary?: boolean }

export function parsePatch(patch: string): DiffFile[] {
  const files: DiffFile[] = [];
  let cur: DiffFile | null = null;
  let oldNo = 0;
  let newNo = 0;
  for (const raw of patch.split("\n")) {
    if (raw.startsWith("diff --git ")) {
      const m = /^diff --git a\/(.+?) b\/(.+)$/.exec(raw);
      cur = { path: m ? m[2] : raw.slice(11), oldPath: m ? m[1] : undefined, adds: 0, dels: 0, lines: [] };
      files.push(cur);
      continue;
    }
    if (!cur) {
      if (raw.startsWith("--- ") || raw.startsWith("+++ ")) {
        cur = { path: "", adds: 0, dels: 0, lines: [] };
        files.push(cur);
      } else continue;
    }
    if (raw.startsWith("+++ ")) {
      const p = raw.slice(4).replace(/^b\//, "");
      if (p !== "/dev/null") cur.path = p;
      continue;
    }
    if (raw.startsWith("--- ")) { if (!cur.path) cur.path = raw.slice(4).replace(/^a\//, ""); continue; }
    if (raw.startsWith("Binary files")) { cur.binary = true; continue; }
    if (raw.startsWith("@@")) {
      const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
      oldNo = m ? Number(m[1]) : 0;
      newNo = m ? Number(m[2]) : 0;
      cur.lines.push({ kind: "hunk", text: raw });
      continue;
    }
    if (/^(index |new file mode|deleted file mode|similarity index|rename (from|to)|old mode|new mode)/.test(raw)) continue;
    if (!cur.lines.length) continue;
    if (raw.startsWith("+")) { cur.lines.push({ kind: "add", text: raw.slice(1), newNo: newNo++ }); cur.adds++; }
    else if (raw.startsWith("-")) { cur.lines.push({ kind: "del", text: raw.slice(1), oldNo: oldNo++ }); cur.dels++; }
    else if (raw.startsWith(" ")) cur.lines.push({ kind: "ctx", text: raw.slice(1), oldNo: oldNo++, newNo: newNo++ });
    else if (raw.startsWith("\\")) cur.lines.push({ kind: "meta", text: raw });
  }
  return files.filter((f) => f.path);
}

export interface WordPart { text: string; changed: boolean }

/** Word-level highlight for a deleted/added line pair (common prefix and suffix stay plain). */
export function wordDiff(a: string, b: string): [WordPart[], WordPart[]] {
  let p = 0;
  while (p < a.length && p < b.length && a[p] === b[p]) p++;
  while (p > 0 && /\w/.test(a[p - 1] ?? "") && /\w/.test(a[p] ?? "")) p--;
  let s = 0;
  while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
  while (s > 0 && /\w/.test(a[a.length - s] ?? "") && /\w/.test(a[a.length - s - 1] ?? "")) s--;
  const split = (x: string): WordPart[] => [
    { text: x.slice(0, p), changed: false },
    { text: x.slice(p, x.length - s), changed: true },
    { text: x.slice(x.length - s), changed: false },
  ].filter((w) => w.text.length > 0);
  return [split(a), split(b)];
}

/** Characters that change how code reads without being visible (R2-007 FactChip). */
export const BIDI_RE = /[‪-‮⁦-⁩​-‏﻿]/;
