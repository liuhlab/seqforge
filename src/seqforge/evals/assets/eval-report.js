/* seqforge eval report — one self-contained script, inlined at render.
 *
 * First-party, no framework, no network. It drives exactly two things, and the page is already
 * correct without either: the theme follows `prefers-color-scheme` on its own, and every case card
 * is a <details> whose open/closed state is set as an attribute at render time. So a browser with
 * JS disabled, or a printed page, still reads.
 *
 *   1. The light/dark toggle, which stamps `data-theme` on <html> — the same attribute a host with
 *      its own toggle sets, and the stylesheet lets it win over the media query in both directions.
 *   2. A "failures only" filter, which hides CORRECT cases and nothing else. Skips stay visible: an
 *      unreachable package is a real state of the benchmark tier, and a filter that swallows it is
 *      how a tier quietly shrinks without anyone noticing.
 */
(function () {
  "use strict";

  var root = document.documentElement;
  var KEY = "seqforge-eval-theme";

  function currentTheme() {
    var explicit = root.getAttribute("data-theme");
    if (explicit) return explicit;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  function applyTheme(theme) {
    root.setAttribute("data-theme", theme);
    try { localStorage.setItem(KEY, theme); } catch (e) { /* file:// with storage denied */ }
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.textContent = theme === "dark" ? "☀" : "☽";
  }

  function initTheme() {
    var stored = null;
    try { stored = localStorage.getItem(KEY); } catch (e) { /* ignore */ }
    if (stored) root.setAttribute("data-theme", stored);
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    btn.textContent = currentTheme() === "dark" ? "☀" : "☽";
    btn.addEventListener("click", function () {
      applyTheme(currentTheme() === "dark" ? "light" : "dark");
    });
  }

  function initFilter() {
    var box = document.getElementById("fail-only");
    if (!box) return;
    var note = document.getElementById("no-failures");
    var interesting = document.querySelectorAll('.case:not([data-level="ok"])');

    box.addEventListener("change", function () {
      document.body.setAttribute("data-filter", box.checked ? "fail" : "all");
      if (note) note.style.display = box.checked && interesting.length === 0 ? "block" : "none";
      if (!box.checked) return;
      // Open what survived, so the filtered view needs no second click.
      Array.prototype.forEach.call(interesting, function (el) {
        if (el.tagName === "DETAILS") el.open = true;
      });
    });
  }

  initTheme();
  initFilter();
})();
