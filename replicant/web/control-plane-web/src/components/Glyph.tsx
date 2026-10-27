// Status glyphs (design/prototype/app.js): 12 px SVG shapes; the adjacent label carries the meaning.
import type { Family } from "../lib/status";

const G: Record<string, string> = {
  check: '<circle cx="6" cy="6" r="5.5" fill="currentColor"/><path d="M3.4 6.2l1.7 1.7 3.5-3.6" stroke="var(--c-bg)" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  cross: '<rect x="0.5" y="0.5" width="11" height="11" rx="2" fill="currentColor"/><path d="M4 4l4 4M8 4l-4 4" stroke="var(--c-bg)" stroke-width="1.5" stroke-linecap="round"/>',
  diamond: '<path d="M6 0.6L11.4 6 6 11.4 0.6 6z" fill="currentColor"/>',
  "diamond-hollow": '<path d="M6 1.4L10.6 6 6 10.6 1.4 6z" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  "ring-spin": '<circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" stroke-opacity="0.3" stroke-width="1.5"/><path d="M6 1.25A4.75 4.75 0 0 1 10.75 6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="6" cy="6" r="1.6" fill="currentColor"/>',
  "ring-dotted": '<circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="1.6 2"/>',
  "ring-half": '<circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6 1.25A4.75 4.75 0 0 1 6 10.75z" fill="currentColor"/>',
  bars: '<circle cx="6" cy="6" r="5.5" fill="none" stroke="currentColor" stroke-width="1"/><path d="M4.5 3.8v4.4M7.5 3.8v4.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
  slash: '<circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M2.8 9.2L9.2 2.8" stroke="currentColor" stroke-width="1.5"/>',
  hourglass: '<path d="M2.5 1h7M2.5 11h7M3 1.2c0 3 6 3.2 6 4.8S3 8 3 10.8M9 1.2c0 3-6 3.2-6 4.8s6 2 6 4.8" fill="none" stroke="currentColor" stroke-width="1.3"/>',
  hand: '<rect x="0.5" y="0.5" width="11" height="11" rx="5.5" fill="currentColor"/><path d="M3.5 6h5M6.5 4l2 2-2 2" stroke="var(--c-bg)" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  return: '<circle cx="6" cy="6" r="5.5" fill="currentColor"/><path d="M8.5 7.5H4.2M5.8 5.6L3.9 7.5l1.9 1.9M8.5 7.5V4" stroke="var(--c-bg)" stroke-width="1.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  triangle: '<path d="M6 0.8l5.4 10.2H0.6z" fill="currentColor"/><path d="M6 4.3v3M6 8.9v0.1" stroke="var(--c-bg)" stroke-width="1.4" stroke-linecap="round"/>',
  dot: '<circle cx="6" cy="6" r="4" fill="currentColor"/>',
  "dot-hollow": '<circle cx="6" cy="6" r="3.75" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  plain: '<circle cx="6" cy="6" r="2" fill="currentColor"/>',
};

export function Glyph({ g, family, style }: { g: string; family?: Family; style?: React.CSSProperties }) {
  return (
    <i className={"g" + (family ? ` f-${family}` : "")} data-g={g} style={style}
      dangerouslySetInnerHTML={{ __html: `<svg viewBox="0 0 12 12" aria-hidden="true">${G[g] ?? G.plain}</svg>` }} />
  );
}

const I: Record<string, string> = {
  jobs: '<path d="M2 3.5h10M2 7h10M2 10.5h6" />',
  approvals: '<path d="M7 1.5L12.5 7 7 12.5 1.5 7z"/>',
  releases: '<path d="M2 11.5h10M3.5 9V5.5M7 9V2.5M10.5 9V6.5"/>',
  memory: '<path d="M3 2.5h8v9H3zM5 2.5v9M3 5.5h2M3 8.5h2"/>',
  projects: '<path d="M1.5 3h4l1.2 1.5h5.8v7h-11z"/>',
  admin: '<circle cx="7" cy="7" r="2"/><path d="M7 1.5v2M7 10.5v2M1.5 7h2M10.5 7h2M3.1 3.1l1.4 1.4M9.5 9.5l1.4 1.4M3.1 10.9l1.4-1.4M9.5 4.5l1.4-1.4"/>',
  temporal: '<circle cx="7" cy="7" r="5.2"/><path d="M7 4v3l2 1.5"/>',
  grafana: '<path d="M2 11.5h10M2.5 9l3-3.5 2.5 2 3.5-4.5"/>',
  waiting: '<path d="M1.5 8h3l1 1.8h3l1-1.8h3M1.5 8l1.6-5.5h7.8L12.5 8v3.5h-11z"/>',
  mine: '<circle cx="7" cy="5" r="2.3"/><path d="M2.5 12c.6-2.3 2.4-3.5 4.5-3.5s3.9 1.2 4.5 3.5"/>',
  search: '<circle cx="6" cy="6" r="4.2"/><path d="M9.2 9.2L12.5 12.5"/>',
  steer: '<path d="M1.5 7h8M7 4l3 3-3 3"/>',
  theme: '<circle cx="7" cy="7" r="5"/><path d="M7 2v10a5 5 0 0 0 0-10z" fill="currentColor"/>',
};

export function Icon({ k, className = "ico" }: { k: string; className?: string }) {
  return (
    <svg className={className} viewBox="0 0 14 14" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.25"
      strokeLinejoin="round" strokeLinecap="round" aria-hidden="true" dangerouslySetInnerHTML={{ __html: I[k] ?? "" }} />
  );
}
