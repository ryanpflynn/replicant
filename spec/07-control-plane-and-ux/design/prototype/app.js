// Prototype runtime: theme from ?theme=, shared rail, status glyphs, command palette (⌘K or ?palette=1).
// No framework, no network. Mirrors behaviour specified in ../components.md.
(function () {
  const qs = new URLSearchParams(location.search);
  const theme = qs.get("theme");
  if (theme === "dark" || theme === "light") document.documentElement.dataset.theme = theme;
  const admin = qs.has("admin") ? qs.get("admin") !== "0" : document.body.dataset.admin === "true";

  // ---- Status glyphs (shape carries meaning; colour reinforces) ----
  const G = {
    "check": '<circle cx="6" cy="6" r="5.5" fill="currentColor"/><path d="M3.4 6.2l1.7 1.7 3.5-3.6" stroke="var(--c-bg)" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    "cross": '<rect x="0.5" y="0.5" width="11" height="11" rx="2" fill="currentColor"/><path d="M4 4l4 4M8 4l-4 4" stroke="var(--c-bg)" stroke-width="1.5" stroke-linecap="round"/>',
    "diamond": '<path d="M6 0.6L11.4 6 6 11.4 0.6 6z" fill="currentColor"/>',
    "diamond-hollow": '<path d="M6 1.4L10.6 6 6 10.6 1.4 6z" fill="none" stroke="currentColor" stroke-width="1.5"/>',
    "ring-spin": '<circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" stroke-opacity="0.3" stroke-width="1.5"/><path d="M6 1.25A4.75 4.75 0 0 1 10.75 6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="6" cy="6" r="1.6" fill="currentColor"/>',
    "ring-dotted": '<circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="1.6 2"/>',
    "ring-half": '<circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6 1.25A4.75 4.75 0 0 1 6 10.75z" fill="currentColor"/>',
    "bars": '<circle cx="6" cy="6" r="5.5" fill="none" stroke="currentColor" stroke-width="1"/><path d="M4.5 3.8v4.4M7.5 3.8v4.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
    "slash": '<circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M2.8 9.2L9.2 2.8" stroke="currentColor" stroke-width="1.5"/>',
    "hourglass": '<path d="M2.5 1h7M2.5 11h7M3 1.2c0 3 6 3.2 6 4.8S3 8 3 10.8M9 1.2c0 3-6 3.2-6 4.8s6 2 6 4.8" fill="none" stroke="currentColor" stroke-width="1.3"/>',
    "hand": '<rect x="0.5" y="0.5" width="11" height="11" rx="5.5" fill="currentColor"/><path d="M3.5 6h5M6.5 4l2 2-2 2" stroke="var(--c-bg)" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    "return": '<circle cx="6" cy="6" r="5.5" fill="currentColor"/><path d="M8.5 7.5H4.2M5.8 5.6L3.9 7.5l1.9 1.9M8.5 7.5V4" stroke="var(--c-bg)" stroke-width="1.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    "triangle": '<path d="M6 0.8l5.4 10.2H0.6z" fill="currentColor"/><path d="M6 4.3v3M6 8.9v0.1" stroke="var(--c-bg)" stroke-width="1.4" stroke-linecap="round"/>',
    "dot": '<circle cx="6" cy="6" r="4" fill="currentColor"/>',
    "dot-hollow": '<circle cx="6" cy="6" r="3.75" fill="none" stroke="currentColor" stroke-width="1.5"/>',
    "plain": '<circle cx="6" cy="6" r="2" fill="currentColor"/>',
  };
  document.querySelectorAll("i.g[data-g]").forEach((el) => {
    el.innerHTML = '<svg viewBox="0 0 12 12" aria-hidden="true">' + (G[el.dataset.g] || G.plain) + "</svg>";
  });

  // ---- Icons for the rail (1.25px strokes, 14px) ----
  const I = {
    jobs: '<path d="M2 3.5h10M2 7h10M2 10.5h6" />',
    approvals: '<path d="M7 1.5L12.5 7 7 12.5 1.5 7z"/>',
    releases: '<path d="M2 11.5h10M3.5 9V5.5M7 9V2.5M10.5 9V6.5"/>',
    registries: '<rect x="2" y="2" width="4" height="4"/><rect x="8" y="2" width="4" height="4"/><rect x="2" y="8" width="4" height="4"/><rect x="8" y="8" width="4" height="4"/>',
    cost: '<path d="M7 1.5v11M10 4.2C9.5 3.3 8.4 2.8 7 2.8c-1.8 0-3 .9-3 2.1 0 2.9 6 1.4 6 4.3 0 1.2-1.3 2-3 2-1.5 0-2.7-.6-3.2-1.6"/>',
    memory: '<path d="M3 2.5h8v9H3zM5 2.5v9M3 5.5h2M3 8.5h2"/>',
    admin: '<circle cx="7" cy="7" r="2"/><path d="M7 1.5v2M7 10.5v2M1.5 7h2M10.5 7h2M3.1 3.1l1.4 1.4M9.5 9.5l1.4 1.4M3.1 10.9l1.4-1.4M9.5 4.5l1.4-1.4"/>',
    temporal: '<circle cx="7" cy="7" r="5.2"/><path d="M7 4v3l2 1.5"/>',
    mine: '<circle cx="7" cy="5" r="2.3"/><path d="M2.5 12c.6-2.3 2.4-3.5 4.5-3.5s3.9 1.2 4.5 3.5"/>',
  };
  const icon = (k) => '<svg class="ico" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true">' + I[k] + "</svg>";

  const page = document.body.dataset.page;
  const rail = document.querySelector(".rail[data-auto]");
  if (rail) {
    const item = (k, label, href, count, cls, key) =>
      `<a href="${href}${theme ? "?theme=" + theme + (admin ? "&admin=1" : "&admin=0") : ""}" class="${page === k ? "on" : ""}" ${page === k ? 'aria-current="page"' : ""}>${icon(k === "myjobs" ? "mine" : k)}<span>${label}</span>${count != null ? `<span class="count ${cls || ""}">${count}</span>` : ""}${key ? `<kbd>${key}</kbd>` : ""}</a>`;
    rail.innerHTML = `
      <div class="brand"><span class="mark" aria-hidden="true"><i></i><i></i><i></i><i></i></span><span class="word">replicant</span><span class="env prod" title="Production environment">prod</span></div>
      <nav class="nav" aria-label="Primary">
        <div class="group caps">Work</div>
        ${item("jobs", "Jobs", "job-list.html", 148)}
        ${item("myjobs", "My jobs", "job-list.html", 6)}
        ${item("approvals", "Approvals", "approvals.html", 4, "attn")}
        <div class="group caps">Platform</div>
        ${item("releases", "Releases", "releases.html", null)}
        ${item("registries", "Registries", "#", null)}
        ${item("cost", "Cost &amp; quotas", "#", null)}
        ${admin ? `<div class="group caps">Replicant admin</div>${item("memory", "Memory", "memory.html", 23)}${item("admin", "Admin", "#", null)}` : `<div class="group caps">Tenant</div>${item("admin", "Tenant settings", "#", null)}`}
        <div class="group caps">Operators</div>
        ${item("temporal", "Temporal UI", "#", "↗")}
      </nav>
      <div class="me"><span class="avatar">${admin ? "SW" : "PR"}</span><span class="who small"><span>${admin ? "Sam Whitfield" : "Priya Raman"}</span><br><span class="t3 micro">${admin ? "replicant-admins · platform" : "payments · approver"}</span></span></div>`;
  }

  document.querySelectorAll("i.g[data-g]").forEach((el) => {
    if (!el.firstChild) el.innerHTML = '<svg viewBox="0 0 12 12" aria-hidden="true">' + (G[el.dataset.g] || G.plain) + "</svg>";
  });
  // ---- Command palette ----
  const open = () => document.body.classList.add("palette-open");
  const close = () => document.body.classList.remove("palette-open");
  if (qs.get("palette") === "1") open();
  if (qs.get("dialog")) document.body.classList.add("dialog-open");
  // Screen states (R2-036): elements with data-show="a b" render only for ?state=a|b; data-hide hides them.
  const st = qs.get("state") || "default";
  document.querySelectorAll("[data-show]").forEach((el) => { if (!el.dataset.show.split(" ").includes(st)) el.remove(); });
  document.querySelectorAll("[data-hide]").forEach((el) => { if (el.dataset.hide.split(" ").includes(st)) el.remove(); });
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); document.body.classList.toggle("palette-open"); }
    if (e.key === "Escape") close();
  });
  document.querySelector(".scrim")?.addEventListener("click", close);

  // ---- Chart hover (releases): nearest-x crosshair tooltip ----
  document.querySelectorAll("svg[data-series]").forEach((svg) => {
    const tip = document.getElementById(svg.dataset.tip);
    const data = JSON.parse(svg.dataset.series);
    const line = svg.querySelector(".xhair");
    svg.addEventListener("mousemove", (e) => {
      const r = svg.getBoundingClientRect();
      const x = ((e.clientX - r.left) / r.width) * 720;
      const i = Math.max(0, Math.min(data.length - 1, Math.round((x - 36) / ((720 - 48) / (data.length - 1)))));
      const px = 36 + i * ((720 - 48) / (data.length - 1));
      line.setAttribute("x1", px); line.setAttribute("x2", px); line.style.display = "";
      tip.style.display = "block"; tip.style.left = (px / 720) * r.width + 12 + "px";
      tip.innerHTML = `<b>${data[i][0]}</b><br>rb_ds5p3wmcj · ${data[i][1].toFixed(1)}%<br>rb_k7wq0f2e9n · ${data[i][2].toFixed(1)}%`;
    });
    svg.addEventListener("mouseleave", () => { tip.style.display = "none"; line.style.display = "none"; });
  });
})();
