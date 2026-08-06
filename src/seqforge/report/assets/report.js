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
    document.querySelectorAll(".smp-toggle").forEach(function (cell) {
      function toggle() {
        var target = document.getElementById(cell.getAttribute("data-target"));
        if (!target) return;
        var open = target.hasAttribute("hidden");
        if (open) target.removeAttribute("hidden");
        else target.setAttribute("hidden", "");
        cell.setAttribute("aria-expanded", open ? "true" : "false");
        var caret = cell.querySelector(".smp-caret");
        if (caret) caret.textContent = open ? "▾" : "▸";
      }
      cell.addEventListener("click", toggle);
      cell.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
      });
    });
  }

  // ---- samples table paging ---------------------------------------------------------------------
  // A plate of ninety-six samples was five thousand pixels of table, and a reader who wanted sample 40
  // scrolled past thirty-nine to reach it. This shows one page of rows at a time.
  //
  // It HIDES, it never drops: every row is in the HTML whatever page is showing, so the file is as
  // complete offline as it was, "Rows: All" is one click, and a browser find that lands on a hidden
  // row still has the row to land on once the reader shows it. The bar itself is `hidden` in the
  // markup and unhidden here, so a page opened without this script is the page as it always was.
  function initSamplePagers() {
    document.querySelectorAll("[data-pager]").forEach(function (bar) {
      var body = document.getElementById(bar.getAttribute("data-pager"));
      if (!body) return;

      // One sample is a PAIR of rows — the summary and its files drawer — and only the first carries
      // `data-smp`. Grouping as "a marked row plus everything after it until the next" means the two
      // move together and the pager never counts rows.
      var groups = [];
      [].forEach.call(body.rows, function (tr) {
        if (tr.hasAttribute("data-smp")) groups.push([tr]);
        else if (groups.length) groups[groups.length - 1].push(tr);
      });
      if (!groups.length) return;

      var status = bar.querySelector("[data-pager-status]");
      var nav = bar.querySelector("[data-pager-nav]");
      var at = bar.querySelector("[data-pager-at]");
      var picker = bar.querySelector("[data-pager-size]");
      var steps = bar.querySelectorAll("[data-pager-step]");
      var page = 0;

      function perPage() {
        var n = parseInt(picker.value, 10);
        return n > 0 ? n : groups.length; // "All" is 0 in the markup, and every row here
      }

      function apply() {
        var per = perPage();
        var pages = Math.ceil(groups.length / per);
        page = Math.min(Math.max(page, 0), pages - 1);
        var first = page * per;
        var last = Math.min(first + per, groups.length);
        groups.forEach(function (rows, i) {
          var off = i < first || i >= last;
          rows.forEach(function (tr) { tr.classList.toggle("smp-off", off); });
        });
        // `.smp-off` and not the `hidden` attribute: `hidden` is the drawer's own state on the second
        // row of every pair, and one attribute meaning two things would close a reader's open drawer
        // every time they paged past it and back.
        status.textContent =
          "Showing " + (first + 1) + "–" + last + " of " + groups.length + " samples";
        at.textContent = "Page " + (page + 1) + " of " + pages;
        nav.hidden = pages < 2; // "Page 1 of 1" beside two dead arrows says nothing
        steps.forEach(function (btn) {
          var to = page + parseInt(btn.getAttribute("data-pager-step"), 10);
          btn.disabled = to < 0 || to >= pages;
        });
        closeProvenance(); // it is pinned beside a cell that may not be on this page any more
      }

      // The top of the table, landed under the sticky band rather than beneath it: a reader who
      // pressed Next at the bottom of one page is asking for the top of the next. The band's height
      // is MEASURED and never assumed — the whole reason the header and the tab bar are one sticky
      // element is that no number for it can be written down and stay true.
      function toTop() {
        var region = body.closest(".sf-scroll-x") || body;
        var band = document.querySelector("[data-sticky-band]");
        var top =
          region.getBoundingClientRect().top +
          window.scrollY -
          (band ? band.getBoundingClientRect().height : 0) -
          8;
        window.scrollTo(0, Math.max(0, top));
      }

      steps.forEach(function (btn) {
        btn.addEventListener("click", function () {
          page += parseInt(btn.getAttribute("data-pager-step"), 10);
          apply();
          toTop();
        });
      });
      picker.addEventListener("change", function () {
        page = 0; // 25→100 keeps the rows you were looking at; anything else lands you nowhere
        apply();
      });

      bar.hidden = false;
      apply();
    });
  }

  // ---- provenance popover -----------------------------------------------------------------------
  // A native title="" tooltip is transient and can't be selected or copied. Instead, a click pins a
  // small card next to the cell with the provenance as real, selectable text plus a Copy button. It
  // lives at the top of <body> (position:fixed) so the samples table's horizontal scroll never clips it.
  // Three kinds of cell opt in, and they are the three places this page has a sentence worth copying:
  // a sample attribute (.basis-cell) carries where a value came from and the quote that says so; an
  // evidence-matrix cell that was forbidden or never scored (.mx-cell) carries WHY, which used to be a
  // title="" a reader could neither select nor keep open; and a Results metric COLUMN HEADER
  // (.metric-head) carries what the number means — on the header, not the cell, because the hint
  // describes the metric and is stored once per column instead of once per sample.
  var CELL_SEL = ".basis-cell, .mx-cell, .metric-head";

  // The one handle out of the popover's closure. Turning a sample table's page can take the cell a
  // popover is pinned beside off the page, and a card left floating beside nothing is worse than no
  // card; `initProvPopover` assigns the real closer over this the moment it runs.
  var closeProvenance = function () {};

  // Whether a cell has anything to say. Formerly `!cell.classList.contains("empty")`, which made a
  // presentation class into a behaviour switch: an empty attribute cell had to keep wearing `.empty`
  // or it would have opened a popover with nothing in it. The real condition was always "does this
  // cell carry provenance", and `data-key` is where provenance starts — so a cell opts IN by carrying
  // data, and no styling decision can turn the popover on or off by accident.
  function hasProvenance(cell) {
    return !!(cell && cell.getAttribute("data-key"));
  }

  function initProvPopover() {
    var pop = null;
    var openCell = null;

    function close() {
      if (pop) { pop.remove(); pop = null; }
      if (openCell) { openCell.removeAttribute("aria-expanded"); openCell = null; }
    }
    closeProvenance = close;

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
      return node && node.closest ? node.closest(CELL_SEL) : null;
    }

    document.addEventListener("click", function (e) {
      if (pop && e.target.closest && e.target.closest(".prov-pop")) return; // clicks inside stay open
      var cell = cellOf(e.target);
      if (hasProvenance(cell)) {
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
      if ((e.key === "Enter" || e.key === " ") && active && active.matches && active.matches(CELL_SEL) && hasProvenance(active)) {
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
    initProvPopover(); // before the pagers: it is what gives them a popover to close
    initSamplePagers();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
