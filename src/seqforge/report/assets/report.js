/* seqforge report — one self-contained script, inlined at render.
 *
 * Drives the tab shell, the assay switcher, the light/dark toggle, and the Mermaid flow charts. No
 * network, no framework: mermaid is inlined above this, and everything else is a few DOM handlers.
 */
(function () {
  "use strict";

  var root = document.documentElement;

  // ---- theme -------------------------------------------------------------------------------------
  function currentTheme() {
    var explicit = root.getAttribute("data-theme");
    if (explicit) return explicit;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  function applyTheme(theme) {
    root.setAttribute("data-theme", theme);
    try { localStorage.setItem("seqforge-report-theme", theme); } catch (e) {}
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.textContent = theme === "dark" ? "☀️" : "☽";
    renderFlows(theme);
  }

  function initTheme() {
    var stored = null;
    try { stored = localStorage.getItem("seqforge-report-theme"); } catch (e) {}
    if (stored) root.setAttribute("data-theme", stored);
    var btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.textContent = currentTheme() === "dark" ? "☀️" : "☽";
      btn.addEventListener("click", function () {
        applyTheme(currentTheme() === "dark" ? "light" : "dark");
      });
    }
  }

  // ---- tabs + assay switch ----------------------------------------------------------------------
  var state = { assay: 0, tab: "overview" };

  function sync() {
    var sections = document.querySelectorAll("section.assay");
    sections.forEach(function (sec, i) {
      sec.style.display = i === state.assay ? "" : "none";
      if (i !== state.assay) return;
      sec.querySelectorAll(".pane").forEach(function (p) {
        p.classList.toggle("active", p.getAttribute("data-tab") === state.tab);
      });
    });
    document.querySelectorAll(".tab").forEach(function (t) {
      t.classList.toggle("active", t.getAttribute("data-tab") === state.tab);
    });
  }

  function initTabs() {
    document.querySelectorAll(".tab").forEach(function (t) {
      t.addEventListener("click", function () {
        state.tab = t.getAttribute("data-tab");
        sync();
      });
    });
    var sel = document.getElementById("assay-select");
    if (sel) {
      sel.addEventListener("change", function () {
        state.assay = parseInt(sel.value, 10) || 0;
        sync();
      });
    }
    sync();
  }

  // ---- mermaid -----------------------------------------------------------------------------------
  var flowSeq = 0;

  function renderFlows(theme) {
    if (!window.mermaid) return;
    try {
      window.mermaid.initialize({
        startOnLoad: false,
        securityLevel: "loose",
        theme: theme === "dark" ? "dark" : "default",
        flowchart: { htmlLabels: true, curve: "basis", useMaxWidth: true },
        themeVariables: { fontFamily: "-apple-system, Segoe UI, Roboto, sans-serif" },
      });
    } catch (e) {}
    document.querySelectorAll('script[type="text/x-mermaid"]').forEach(function (node) {
      var target = document.getElementById(node.getAttribute("data-target"));
      if (!target) return;
      var source = node.textContent;
      var id = "mmd-" + flowSeq++;
      try {
        var out = window.mermaid.render(id, source);
        if (out && typeof out.then === "function") {
          out.then(function (r) { target.innerHTML = r.svg; }).catch(function () {
            target.innerHTML = '<pre class="mono">' + escapeHtml(source) + "</pre>";
          });
        } else if (out && out.svg) {
          target.innerHTML = out.svg;
        }
      } catch (e) {
        target.innerHTML = '<pre class="mono">' + escapeHtml(source) + "</pre>";
      }
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
    });
  }

  // ---- boot --------------------------------------------------------------------------------------
  function boot() {
    initTheme();
    initTabs();
    renderFlows(currentTheme());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
