/* Stockwise: small conveniences on top of the server-rendered pages (no build step).
   - js-multi selects become searchable multi-pick dropdowns (Tom Select)
   - js-table tables get a search box, click-to-sort columns, 25 rows at a time and a CSV download
   Re-applied after every HTMX swap, so filtered pages keep them. */
(function () {
  const PAGE = 25;

  // ₹1.2 Cr, ₹45.3 L, ₹48K, 1,234, 12.5%, 0.52×, 05 Nov 2026 -> numbers that sort correctly
  const MONTHS = {Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5, Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11};
  function sortValue(text) {
    const t = text.trim();
    if (t === "" || t === "–" || t === "-") return null;
    const d = t.match(/^(\d{1,2}) (\w{3}) (\d{4})/);
    if (d && d[2] in MONTHS) return new Date(+d[3], MONTHS[d[2]], +d[1]).getTime();
    const m = t.replace(/[₹,×%≈+\s]/g, "").match(/^(-?\d+(?:\.\d+)?)(Cr|L|K)?/);
    if (m && /^[₹\d\-+≈.,\s]/.test(t)) {
      const mult = {Cr: 1e7, L: 1e5, K: 1e3}[m[2]] || 1;
      return parseFloat(m[1]) * mult;
    }
    return t.toLowerCase();
  }

  function enhanceTable(table) {
    if (table.dataset.enhanced) return;
    table.dataset.enhanced = "1";
    const tbody = table.tBodies[0];
    if (!tbody) return;
    const rows = Array.from(tbody.rows);
    const headers = Array.from(table.tHead ? table.tHead.rows[0].cells : []);
    let shown = PAGE, query = "", sortCol = -1, sortDir = 1;

    // toolbar: search, count, download
    const bar = document.createElement("div");
    bar.className = "table-tools d-flex flex-wrap align-items-center gap-2 px-3 py-2 border-bottom";
    bar.innerHTML = `<div class="input-icon flex-grow-1" style="max-width: 18rem">
        <span class="input-icon-addon"><i class="ti ti-search"></i></span>
        <input type="search" class="form-control form-control-sm" placeholder="Search this table" aria-label="Search this table"></div>
      <span class="small text-secondary count"></span>
      <button type="button" class="btn btn-sm btn-ghost-secondary ms-auto csv" title="Download as a spreadsheet (CSV)">
        <i class="ti ti-download"></i> CSV</button>`;
    const wrap = table.closest(".table-responsive") || table;
    wrap.parentNode.insertBefore(bar, wrap);
    const more = document.createElement("div");
    more.className = "text-center py-2 border-top table-more";
    more.innerHTML = `<button type="button" class="btn btn-sm btn-outline-primary">Show 25 more</button>
      <button type="button" class="btn btn-sm btn-ghost-secondary all">Show all</button>`;
    wrap.parentNode.insertBefore(more, wrap.nextSibling);

    function matching() {
      if (!query) return rows;
      const q = query.toLowerCase();
      return rows.filter(r => r.textContent.toLowerCase().includes(q));
    }
    function render() {
      const list = matching();
      rows.forEach(r => (r.style.display = "none"));
      list.forEach((r, i) => (r.style.display = i < shown ? "" : "none"));
      bar.querySelector(".count").textContent =
        list.length > shown ? `${shown} of ${list.length} rows` : `${list.length} row${list.length === 1 ? "" : "s"}`;
      more.style.display = list.length > shown ? "" : "none";
    }
    bar.querySelector("input").addEventListener("input", e => { query = e.target.value; shown = PAGE; render(); });
    more.querySelector(".btn-outline-primary").addEventListener("click", () => { shown += PAGE; render(); });
    more.querySelector(".all").addEventListener("click", () => { shown = rows.length; render(); });

    headers.forEach((th, col) => {
      th.classList.add("sortable");
      th.title = "Sort";
      th.addEventListener("click", () => {
        sortDir = sortCol === col ? -sortDir : (th.classList.contains("text-end") ? -1 : 1);   // numbers: largest first
        sortCol = col;
        headers.forEach(h => h.classList.remove("sorted-asc", "sorted-desc"));
        th.classList.add(sortDir > 0 ? "sorted-asc" : "sorted-desc");
        rows.sort((a, b) => {
          const x = sortValue(a.cells[col] ? a.cells[col].textContent : ""), y = sortValue(b.cells[col] ? b.cells[col].textContent : "");
          if (x === null) return 1;
          if (y === null) return -1;
          return (x > y ? 1 : x < y ? -1 : 0) * sortDir;
        });
        rows.forEach(r => tbody.appendChild(r));
        render();
      });
    });

    bar.querySelector(".csv").addEventListener("click", () => {
      const esc = v => `"${String(v).replace(/"/g, '""').trim()}"`;
      const lines = [headers.map(h => esc(h.textContent)).join(",")]
        .concat(matching().map(r => Array.from(r.cells).map(c => esc(c.textContent)).join(",")));
      const blob = new Blob(["﻿" + lines.join("\r\n")], {type: "text/csv"});
      const a = document.createElement("a");
      const title = (table.closest(".card")?.querySelector(".card-title")?.textContent || "table").trim();
      a.href = URL.createObjectURL(blob);
      a.download = title.replace(/[^\w]+/g, "_").slice(0, 60) + ".csv";
      a.click();
    });
    if (rows.length <= 8) { bar.querySelector(".input-icon").style.display = "none"; }
    render();
  }

  function enhance(root) {
    root.querySelectorAll("table.js-table").forEach(enhanceTable);
    if (window.TomSelect) {
      root.querySelectorAll("select.js-multi").forEach(sel => {
        if (sel.tomselect) return;
        const ts = new TomSelect(sel, {plugins: ["remove_button"], maxOptions: 500, hidePlaceholder: true,
                                       placeholder: sel.dataset.placeholder || "All"});
        if (ts.control_input && sel.getAttribute("aria-label")) ts.control_input.setAttribute("aria-label", sel.getAttribute("aria-label"));
      });
    }
  }

  document.addEventListener("DOMContentLoaded", () => enhance(document));
  document.body && document.body.addEventListener("htmx:afterSettle", e => enhance(e.target));
  document.addEventListener("htmx:afterSettle", e => enhance(e.target));
})();

/* Ask Stockwise side panel (templates/_ask_panel.html): loads its conversation the first time it opens, sends the
   current page with each question, Enter sends.
   Wide screens (DOCK and up): the page makes room for the panel and stays usable beside it; the panel stays open
   across page changes (remembered for this browser tab). Narrower screens: the panel sits over a dimmed, frozen
   page (click the dimmed part to close) and doesn't reopen on its own. Scrolling the chat never scrolls the page. */
(function () {
  const KEY = "swAskOpen";
  const DOCK = window.matchMedia("(min-width: 1440px)");
  function chartsResize() { setTimeout(() => window.dispatchEvent(new Event("resize")), 260); }
  function remember(v) { try { v ? sessionStorage.setItem(KEY, "1") : sessionStorage.removeItem(KEY); } catch (e) {} }
  function wasOpen() { try { return sessionStorage.getItem(KEY) === "1"; } catch (e) { return false; } }
  function scrollDown() { const b = document.getElementById("ask-body"); if (b) b.scrollTop = b.scrollHeight; }

  document.addEventListener("DOMContentLoaded", () => {
    const panel = document.getElementById("ask-panel");
    if (!panel) return;
    const form = document.getElementById("ask-form"), box = document.getElementById("ask-question");
    let loaded = false;
    const backdrop = document.createElement("div");
    backdrop.className = "sw-ask-backdrop";
    backdrop.setAttribute("aria-hidden", "true");
    document.body.appendChild(backdrop);
    const close = () => (window.bootstrap && bootstrap.Offcanvas)
      ? bootstrap.Offcanvas.getOrCreateInstance(panel).hide() : panel.querySelector(".btn-close").click();
    backdrop.addEventListener("click", close);
    document.addEventListener("keydown", e => { if (e.key === "Escape" && panel.classList.contains("show")) close(); });

    panel.addEventListener("show.bs.offcanvas", () => {
      document.body.classList.add("sw-ask-open");
      if (!loaded) { loaded = true; htmx.trigger(document.body, "sw-ask-load"); }
      remember(true);
      if (DOCK.matches) chartsResize();
    });
    panel.addEventListener("shown.bs.offcanvas", () => { scrollDown(); box.focus(); });
    panel.addEventListener("hidden.bs.offcanvas", () => {
      document.body.classList.remove("sw-ask-open");
      remember(false);
      if (DOCK.matches) chartsResize();
    });
    DOCK.addEventListener("change", () => { if (panel.classList.contains("show")) chartsResize(); });

    // the chat scrolls on its own: at its top or bottom, and over the header or the question box, the page stays put
    panel.addEventListener("wheel", e => {
      const area = e.target.closest("#ask-body, textarea, .dropdown-menu");
      if (!area) { e.preventDefault(); return; }
      const up = e.deltaY < 0, down = e.deltaY > 0;
      if ((up && area.scrollTop <= 0) || (down && area.scrollTop + area.clientHeight >= area.scrollHeight - 1)) e.preventDefault();
    }, {passive: false});

    // reopen without animation if it was open on the previous page (only where the page stays usable beside it)
    if ((wasOpen() && DOCK.matches) || new URLSearchParams(location.search).get("ask") === "1") {
      panel.classList.add("show-instant");
      const fab = document.querySelector(".sw-ask-fab");
      if (window.bootstrap && bootstrap.Offcanvas) bootstrap.Offcanvas.getOrCreateInstance(panel).show();
      else if (fab) fab.click();
      setTimeout(() => panel.classList.remove("show-instant"), 400);
    }

    form.addEventListener("htmx:configRequest", e => {
      e.detail.parameters.page = location.pathname + location.search;
      e.detail.parameters.title = document.title;
    });
    form.addEventListener("htmx:afterRequest", e => { if (e.detail.successful) { box.value = ""; setTimeout(scrollDown, 50); } });
    box.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (box.value.trim()) htmx.trigger(form, "submit"); }
    });
    panel.addEventListener("click", e => {
      const ex = e.target.closest(".sw-ask-example");
      if (ex) { box.value = ex.textContent.trim(); htmx.trigger(form, "submit"); }
    });
    document.body.addEventListener("htmx:afterSwap", e => { if (e.target.id === "ask-body") setTimeout(scrollDown, 30); });
    // keep the newest text in view while an answer streams in, unless the person has scrolled up to read
    const body = document.getElementById("ask-body");
    let pinned = true;
    body.addEventListener("scroll", () => { pinned = body.scrollHeight - body.scrollTop - body.clientHeight < 80; });
    document.body.addEventListener("htmx:beforeSwap", e => {
      if (e.target.closest && e.target.closest("#ask-body")) pinned = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
    });
    document.body.addEventListener("htmx:afterSettle", e => {
      if (pinned && e.target.closest && e.target.closest("#ask-body")) scrollDown();
    });
  });
})();

/* A request the person's role doesn't allow (web/roles.py) answers with an "sw-refused" event: show it briefly. */
document.body && document.body.addEventListener("sw-refused", e => {
  const box = document.createElement("div");
  box.className = "sw-toast";
  box.setAttribute("role", "alert");
  box.textContent = (e.detail && e.detail.value) || "Your role can't do this.";
  document.body.appendChild(box);
  setTimeout(() => box.remove(), 5000);
});

/* Ideal stock tables (templates/_ideal_editor.html): the suggested numbers are filled in; changing one highlights it
   and shows the save bar (count, one reason, quick reasons, Undo). Leaving with unsaved numbers asks first. */
(function () {
  function refresh(form) {
    let n = 0;
    form.querySelectorAll(".ideal-input").forEach(i => {
      const changed = i.value.trim() !== i.dataset.orig;
      i.classList.toggle("is-changed", changed);
      if (changed) n++;
    });
    const bar = form.querySelector(".ideal-savebar");
    if (!bar) return;
    bar.hidden = n === 0;
    bar.querySelector(".ideal-count").textContent = n === 1 ? "1 change" : n + " changes";
    form.dataset.dirty = n ? "1" : "";
  }
  document.addEventListener("input", e => {
    if (e.target.matches && e.target.matches(".ideal-input")) refresh(e.target.closest("form"));
  });
  document.addEventListener("click", e => {
    const chip = e.target.closest && e.target.closest(".ideal-chip");
    if (chip) {
      const box = chip.closest("form").querySelector(".ideal-reason");
      box.value = chip.dataset.reason; box.focus();
    }
    const undo = e.target.closest && e.target.closest(".ideal-undo");
    if (undo) {
      const form = undo.closest("form");
      form.querySelectorAll(".ideal-input").forEach(i => (i.value = i.dataset.orig));
      refresh(form);
    }
  });
  document.addEventListener("wheel", e => {            // scrolling the page over a number box must not change it
    if (document.activeElement && document.activeElement.matches(".ideal-input") && e.target === document.activeElement) {
      document.activeElement.blur();
    }
  }, {passive: true});
  document.addEventListener("htmx:beforeRequest", e => {
    const form = e.target.closest && e.target.closest(".ideal-editor");
    if (form) form.dataset.dirty = "";               // saving: don't ask about leaving
  });
  document.addEventListener("htmx:afterRequest", e => {
    const form = e.target.closest && e.target.closest(".ideal-editor");
    if (form && !e.detail.xhr.getResponseHeader("HX-Redirect")) refresh(form);   // not saved: still unsaved
  });
  window.addEventListener("beforeunload", e => {
    if (document.querySelector(".ideal-editor[data-dirty='1']")) { e.preventDefault(); e.returnValue = ""; }
  });
})();
