/* seqforge report — one self-contained script, inlined at render.
 *
 * Drives the tab shell, the assay switcher, the light/dark toggle, the sample-row drawers, and the
 * click-to-pin provenance popover. No network, no framework — just a few DOM handlers. The Flow tab is
 * plain HTML cards laid out by CSS, so there is no diagram engine to load.
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

  // ---- sample row expand/collapse ---------------------------------------------------------------
  // The whole first cell is the target (a big, easy click area), not just the little caret.
  function initRowToggles() {
    document.querySelectorAll(".sample-toggle").forEach(function (cell) {
      function toggle() {
        var target = document.getElementById(cell.getAttribute("data-target"));
        if (!target) return;
        var open = target.hasAttribute("hidden");
        if (open) target.removeAttribute("hidden");
        else target.setAttribute("hidden", "");
        cell.setAttribute("aria-expanded", open ? "true" : "false");
        var caret = cell.querySelector(".row-toggle");
        if (caret) caret.textContent = open ? "▾" : "▸";
      }
      cell.addEventListener("click", toggle);
      cell.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
      });
    });
  }

  // ---- provenance popover -----------------------------------------------------------------------
  // A native title="" tooltip is transient and can't be selected or copied. Instead, a click pins a
  // small card next to the cell with the provenance as real, selectable text plus a Copy button. It
  // lives at the top of <body> (position:fixed) so the samples table's horizontal scroll never clips it.
  function initProvPopover() {
    var pop = null;
    var openCell = null;

    function close() {
      if (pop) { pop.remove(); pop = null; }
      if (openCell) { openCell.removeAttribute("aria-expanded"); openCell = null; }
    }

    function fallbackCopy(text) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      try { document.execCommand("copy"); } catch (e) {}
      ta.remove();
    }

    function copyText(text, btn) {
      var flash = function () {
        var prev = btn.textContent;
        btn.textContent = "Copied ✓";
        setTimeout(function () { btn.textContent = prev; }, 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(flash, function () { fallbackCopy(text); flash(); });
      } else {
        fallbackCopy(text);
        flash();
      }
    }

    function el(cls, text) {
      var d = document.createElement("div");
      d.className = cls;
      if (text) d.textContent = text;
      return d;
    }

    function openFor(cell) {
      close();
      var key = cell.getAttribute("data-key") || "";
      var value = cell.getAttribute("data-value") || "";
      var basis = cell.getAttribute("data-basis") || "";
      var source = cell.getAttribute("data-source") || "";
      var quote = cell.getAttribute("data-quote") || "";

      pop = document.createElement("div");
      pop.className = "prov-pop";
      var head = el("pp-head", key + (value ? ": " + value : ""));
      var basisLine = el("pp-basis", basis + (source ? " · " + source : ""));
      pop.appendChild(head);
      pop.appendChild(basisLine);
      if (quote) {
        var q = document.createElement("blockquote");
        q.className = "pp-quote";
        q.textContent = quote;
        pop.appendChild(q);
      }
      var bar = el("pp-bar");
      var copyBtn = document.createElement("button");
      copyBtn.className = "pp-copy";
      copyBtn.type = "button";
      copyBtn.textContent = "Copy";
      copyBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        var text = head.textContent + "\n" + basisLine.textContent + (quote ? "\n“" + quote + "”" : "");
        copyText(text, copyBtn);
      });
      bar.appendChild(copyBtn);
      pop.appendChild(bar);
      document.body.appendChild(pop);
      position(cell);
      openCell = cell;
      cell.setAttribute("aria-expanded", "true");
    }

    function position(cell) {
      var r = cell.getBoundingClientRect();
      pop.style.visibility = "hidden";
      pop.style.left = "0px";
      pop.style.top = "0px";
      var pw = pop.offsetWidth;
      var ph = pop.offsetHeight;
      var left = Math.max(8, Math.min(r.left, window.innerWidth - pw - 12));
      var top = r.bottom + 6;
      if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
      pop.style.left = left + "px";
      pop.style.top = top + "px";
      pop.style.visibility = "visible";
    }

    function cellOf(node) {
      return node && node.closest ? node.closest(".attr-cell") : null;
    }

    document.addEventListener("click", function (e) {
      if (pop && e.target.closest && e.target.closest(".prov-pop")) return; // clicks inside stay open
      var cell = cellOf(e.target);
      if (cell && !cell.classList.contains("empty")) {
        if (cell === openCell) { close(); return; } // toggle off
        openFor(cell);
        e.stopPropagation();
      } else {
        close();
      }
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { close(); return; }
      var active = document.activeElement;
      if ((e.key === "Enter" || e.key === " ") && active && active.classList && active.classList.contains("attr-cell") && !active.classList.contains("empty")) {
        e.preventDefault();
        if (active === openCell) close();
        else openFor(active);
      }
    });

    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
  }

  // ---- boot --------------------------------------------------------------------------------------
  // The Flow tab is plain HTML cards (CSS handles the responsive layout), so there is no diagram
  // engine to drive here — the shell is a few DOM handlers and nothing loads off the network.
  function boot() {
    initTheme();
    initTabs();
    initRowToggles();
    initProvPopover();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
