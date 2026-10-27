// Parse every Mermaid block in WS-04 docs. Setup: npm i mermaid@11.4.1 jsdom@25 ; run: node tools/parse_mermaid.mjs spec/04-memory-and-context
import { JSDOM } from 'jsdom';
import fs from 'fs'; import path from 'path';
const dom = new JSDOM('<!doctype html><html><body></body></html>');
globalThis.window = dom.window; globalThis.document = dom.window.document;
for (const k of ["Element","HTMLElement","Node","SVGElement","navigator"]) { try { globalThis[k] = dom.window[k]; } catch {} }
const { default: mermaid } = await import('mermaid');
mermaid.initialize({ startOnLoad: false });
const dir = process.argv[2]; let ok = 0, bad = 0;
const files = [...fs.readdirSync(dir).filter(f=>f.endsWith('.md')).map(f=>path.join(dir,f)), ...fs.readdirSync(path.join(dir,'adr')).map(f=>path.join(dir,'adr',f))];
for (const f of files) {
  const s = fs.readFileSync(f,'utf8'); const re = /```mermaid\n([\s\S]*?)```/g; let m;
  while ((m = re.exec(s))) { try { await mermaid.parse(m[1]); ok++; } catch (e) { bad++; console.log('FAIL', f, String(e.message||e).slice(0,300)); } }
}
console.log(`mermaid parsed ok=${ok} fail=${bad}`);
