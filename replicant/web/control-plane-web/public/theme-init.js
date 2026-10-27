// Theme before first paint (no flash): the stored choice; otherwise tokens.css follows the OS preference.
try { var t = localStorage.getItem("replicant.theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch (e) { /* storage unavailable */ }
