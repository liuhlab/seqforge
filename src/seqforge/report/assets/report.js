/* seqforge report — one self-contained script, inlined at render.
 *
 * Drives the tab shell, the assay switcher, the light/dark toggle, the sample-row drawers, the paging
 * and sorting of the two sample grids, and the click-to-pin provenance popover. No network, no
 * framework — just a few DOM handlers. The Flow tab is plain HTML cards laid out by CSS, so there is
 * no diagram engine to load.
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

  // ---- sample grids: paging and sorting ---------------------------------------------------------
  // A plate of ninety-six samples was five thousand pixels of table, and a reader who wanted sample 40
  // scrolled past thirty-nine to reach it; the Results grid on the in-house aging plate is 784 rows
  // deep. So a grid shows one page of rows at a time, and lets a reader put the rows they want on the
  // first page by sorting a column.
  //
  // ONE controller owns both, per <tbody>, because they are two halves of one answer: a sort decides
  // the order of every row and paging decides which slice of that order shows. Two handlers could not
  // agree on it — a pager that had grouped the rows once, at load, would go on slicing an order the
  // table had stopped being in, and the symptom would be a page of rows that are not the ones the
  // header says are showing.
  //
  // It HIDES, it never drops: every row is in the HTML whatever page is showing, so the file is as
  // complete offline as it was, "Rows: All" is one click, and a browser find that lands on a hidden
  // row still has the row to land on once the reader shows it. Both controls are `hidden` in the
  // markup and unhidden here, so a page opened without this script is the page as it always was.

  // Digit-aware, because the plate's ids are `day3_CF_1`, `day11_N2_16_2`, ... and a plain string sort
  // reads `day11` as less than `day3`. That IS the arrival order the reader is sorting to escape, so a
  // sort that reproduced it would be a control that does nothing on the one table it was built for.
  var NATURAL =
    window.Intl && Intl.Collator
      ? new Intl.Collator(undefined, { numeric: true }).compare
      : function (a, b) { return a < b ? -1 : a > b ? 1 : 0; };

  // The one `data-sort-col` that is not an index into a row's payload — see `_SORT_BY_ID`.
  var SORT_BY_ID = "id";

  // One sample is a PAIR of rows — the summary and its files drawer — and only the first carries
  // `data-smp`. Grouping as "a marked row plus everything after it until the next" means the two move
  // together, which is what lets a sort reorder SAMPLES rather than rows, and what lets the pager
  // page them without ever counting rows.
  function groupsOf(body) {
    var groups = [];
    [].forEach.call(body.rows, function (tr) {
      if (tr.hasAttribute("data-smp")) groups.push([tr]);
      else if (groups.length) groups[groups.length - 1].push(tr);
    });
    return groups;
  }

  function initGrid(body) {
    // The order the page arrived in, kept for the whole session: it is what the third click of a
    // column restores, and every sort is computed FROM it rather than from the last sort, so the
    // same column and direction always produce the same table.
    var arrival = groupsOf(body);
    if (!arrival.length) return;
    var order = arrival;

    var bar = body.id ? document.querySelector('[data-pager="' + body.id + '"]') : null;
    var table = body.closest("table");
    var carets = table ? [].slice.call(table.querySelectorAll("[data-sort-col]")) : [];
    if (!bar && !carets.length) return;

    var status = bar && bar.querySelector("[data-pager-status]");
    var nav = bar && bar.querySelector("[data-pager-nav]");
    var at = bar && bar.querySelector("[data-pager-at]");
    var picker = bar && bar.querySelector("[data-pager-size]");
    var steps = bar ? [].slice.call(bar.querySelectorAll("[data-pager-step]")) : [];
    var page = 0;

    function perPage() {
      var n = parseInt(picker.value, 10);
      return n > 0 ? n : order.length; // "All" is 0 in the markup, and every row here
    }

    // A table under one page renders no bar at all, and still sorts — so paging is a branch here
    // rather than the reason this function runs.
    function apply() {
      if (bar) {
        var per = perPage();
        var pages = Math.ceil(order.length / per);
        page = Math.min(Math.max(page, 0), pages - 1);
        var first = page * per;
        var last = Math.min(first + per, order.length);
        order.forEach(function (rows, i) {
          var off = i < first || i >= last;
          rows.forEach(function (tr) { tr.classList.toggle("smp-off", off); });
        });
        // `.smp-off` and not the `hidden` attribute: `hidden` is the drawer's own state on the second
        // row of every pair, and one attribute meaning two things would close a reader's open drawer
        // every time they paged past it and back.
        status.textContent =
          "Showing " + (first + 1) + "–" + last + " of " + order.length + " samples";
        at.textContent = "Page " + (page + 1) + " of " + pages;
        nav.hidden = pages < 2; // "Page 1 of 1" beside two dead arrows says nothing
        steps.forEach(function (btn) {
          var to = page + parseInt(btn.getAttribute("data-pager-step"), 10);
          btn.disabled = to < 0 || to >= pages;
        });
      }
      closeProvenance(); // it is pinned beside a cell that may not be on this page any more
    }

    // The top of the table, landed under the sticky band rather than beneath it: a reader who
    // pressed Next at the bottom of one page is asking for the top of the next, and one who just
    // sorted a column is asking for whatever is now at the top of it. The band's height is MEASURED
    // and never assumed — the whole reason the header and the tab bar are one sticky element is that
    // no number for it can be written down and stay true.
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

    // The key rides on the <tr>: one `data-sort` holding this row's RAW values in the order its cells
    // were emitted, comma-separated, empty where the sample has a gap. The rendered text cannot stand
    // in for it — a count reaches the cell as `207.9M`, and the precision a sort needs is gone by
    // then — and the sticky column has no slot at all, because an identifier is not a number.
    function keyOf(group, col) {
      var first = group[0];
      if (col === SORT_BY_ID) {
        // The sticky cell IS the identifier and nothing else, so the key is simply its text: there
        // is no slot to read, because an identifier is not one of the row payload's numbers.
        return first.cells[0].textContent.trim();
      }
      var slot = (first.getAttribute("data-sort") || "").split(",")[col];
      return slot ? parseFloat(slot) : NaN;
    }

    // A gap goes to the END in both directions. A sample that never reported a metric does not hold
    // the largest value in that column, and it does not hold the smallest one either.
    function isGap(k) { return typeof k === "number" && isNaN(k); }

    function sortedBy(col, dir) {
      var keyed = arrival.map(function (g, i) { return { g: g, i: i, k: keyOf(g, col) }; });
      keyed.sort(function (a, b) {
        if (isGap(a.k) || isGap(b.k)) {
          return isGap(a.k) && isGap(b.k) ? a.i - b.i : isGap(a.k) ? 1 : -1;
        }
        var c = typeof a.k === "string" ? NATURAL(a.k, b.k) : a.k - b.k;
        return c ? c * dir : a.i - b.i; // ties keep arrival order, so one click is one table
      });
      return keyed.map(function (e) { return e.g; });
    }

    // Three glyphs for three states, and `aria-sort` on the <th> for the same three: a column header
    // that announced itself as a button would have stopped being a column header, so the state is on
    // the header and the control beside the label.
    function mark(btn, state) {
      var th = btn.closest("th");
      if (th) {
        th.setAttribute(
          "aria-sort",
          state < 0 ? "descending" : state > 0 ? "ascending" : "none"
        );
      }
      btn.textContent = state < 0 ? "▾" : state > 0 ? "▴" : "⇅";
    }

    var sortState = 0; // -1 descending, 1 ascending, 0 the order the page arrived in
    var sortCaret = null;

    carets.forEach(function (btn) {
      btn.hidden = false;
      btn.addEventListener("click", function () {
        // none → descending → ascending → none, and the third click is the way back. Descending
        // first because on a table of metrics the question is nearly always who is highest. One
        // column at a time: a second sort clears the first, because two live sorts is a rule no
        // reader can recover off the page.
        sortState = btn === sortCaret ? (sortState < 0 ? 1 : 0) : -1;
        sortCaret = sortState ? btn : null;
        carets.forEach(function (other) { mark(other, other === sortCaret ? sortState : 0); });

        // EVERY row is sorted and the pager then re-slices — never the visible page, which would
        // sort twenty-five rows and call it the top of the plate.
        order = sortState ? sortedBy(btn.getAttribute("data-sort-col"), sortState) : arrival;
        var frag = document.createDocumentFragment();
        order.forEach(function (rows) {
          rows.forEach(function (tr) { frag.appendChild(tr); });
        });
        body.appendChild(frag); // one reflow, and appending a row already here MOVES it

        page = 0; // the reader asked who is highest, and the answer is on the first page
        apply();
        // Only where the grid pages: a table short enough to render no bar is already showing the
        // reader every row, and there is nowhere for the top to be. The pager's own two calls need
        // no such guard — an arrow only exists on a grid that has a bar to draw it.
        if (bar) toTop();
      });
    });

    if (bar) {
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
    }
    apply();
  }

  // A grid reaches this from either end — it has a pager, or it has sort carets, or both — so both
  // ways in are collected and deduplicated rather than wiring one <tbody> twice.
  function initSampleGrids() {
    var bodies = [];
    function want(body) { if (body && bodies.indexOf(body) < 0) bodies.push(body); }
    document.querySelectorAll("[data-pager]").forEach(function (bar) {
      want(document.getElementById(bar.getAttribute("data-pager")));
    });
    document.querySelectorAll("[data-sort-col]").forEach(function (btn) {
      var table = btn.closest("table");
      want(table && table.tBodies.length ? table.tBodies[0] : null);
    });
    bodies.forEach(initGrid);
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
    initProvPopover(); // before the grids: it is what gives them a popover to close
    initSampleGrids();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
