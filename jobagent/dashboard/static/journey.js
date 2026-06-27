/* JobAgent · "My Journey" — client renderer (zero-build, CDN libs).
   Reads the embedded JSON blob, draws animated charts/rings, fires
   milestone confetti, and wires the functional tabs/filters. */
(function () {
  "use strict";
  var DATA = {};
  try { DATA = JSON.parse(document.getElementById("journey-data").textContent); }
  catch (e) { console.error("journey data parse failed", e); }

  var hasApex = typeof ApexCharts !== "undefined";
  var hasCountUp = typeof countUp !== "undefined" && countUp.CountUp;
  var hasConfetti = typeof confetti !== "undefined";

  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); }
  function ymd(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }

  var STAGE_COLOR = { fresh: "#5ce0a8", follow_up: "#ffcb5c", going_quiet: "#ff8b8b" };
  var STAGE_WORD = { fresh: "Still fresh", follow_up: "Follow up now", going_quiet: "Going quiet" };

  /* ---------- count-up hero/stat numbers ---------- */
  function animateCounts() {
    $$("[data-count]").forEach(function (node) {
      var target = parseFloat(node.getAttribute("data-count")) || 0;
      if (hasCountUp) {
        var cu = new countUp.CountUp(node, target, { duration: 1.6, useEasing: true, separator: "," });
        if (!cu.error) { cu.start(); return; }
      }
      node.textContent = target.toLocaleString();
    });
  }

  /* ---------- stepper fills (scoped per .stepper group) ---------- */
  function fillSteps() {
    $$(".stepper").forEach(function (group) {
      var steps = $$(".step", group);
      var max = Math.max.apply(null, steps.map(function (s) { return parseFloat(s.getAttribute("data-count")) || 0; }).concat([1]));
      steps.forEach(function (s) {
        var c = parseFloat(s.getAttribute("data-count")) || 0;
        requestAnimationFrame(function () { s.style.setProperty("--fill", Math.max(6, Math.round(c / max * 100)) + "%"); });
      });
    });
  }

  /* ---------- wait-time rings (ApexCharts radialBar) ---------- */
  function renderWaiting() {
    var box = $("#waitGrid");
    if (!box) return;
    var w = DATA.waiting || { items: [] };
    var items = w.items || [];
    var horizon = w.noresponse_days || 14;
    var RINGS = 12;
    var rings = items.slice(0, RINGS);
    var rest = items.slice(RINGS);

    rings.forEach(function (it, i) {
      var pct = Math.min(100, Math.round((it.days / horizon) * 100));
      var card = el("div", "wcard s-" + it.stage);
      var ring = el("div", "ring"); ring.id = "ring" + i;
      var meta = el("div", "wmeta");
      meta.innerHTML =
        '<div class="co">' + esc(it.company || "—") + "</div>" +
        '<div class="role">' + esc(it.title || "") + "</div>" +
        '<div class="state s-' + it.stage + '">' + STAGE_WORD[it.stage] + "</div>";
      card.appendChild(ring); card.appendChild(meta); box.appendChild(card);

      if (hasApex) {
        new ApexCharts(ring, {
          chart: { type: "radialBar", height: 64, width: 64, sparkline: { enabled: true }, animations: { enabled: true, speed: 900 } },
          series: [pct],
          colors: [STAGE_COLOR[it.stage]],
          plotOptions: { radialBar: { hollow: { size: "46%" }, track: { background: "#242a40" },
            dataLabels: { name: { show: false }, value: { offsetY: 5, fontSize: "15px", fontWeight: 800, color: "#e7e9f3", formatter: function () { return it.days + "d"; } } } } },
          stroke: { lineCap: "round" }
        }).render();
      } else {
        ring.innerHTML = '<div style="font-weight:800;text-align:center;line-height:64px">' + it.days + "d</div>";
      }
    });

    if (rest.length) {
      var more = el("div", "panel");
      more.style.marginTop = "12px";
      var html = '<div class="label" style="margin-bottom:8px">+' + rest.length + " more waiting (scroll)</div><div class=\"morelist\">";
      rest.forEach(function (it) {
        html += '<div class="prospect"><span class="chip ' +
          (it.stage === "going_quiet" ? "red" : it.stage === "follow_up" ? "amber" : "green") +
          '">' + it.days + "d</span><div class=\"pt\"><div class=\"r\">" + esc(it.company || "—") +
          ' · <span class="muted">' + esc(it.title || "") + "</span></div></div></div>";
      });
      html += "</div>";
      more.innerHTML = html;
      box.parentNode.appendChild(more);
    }
    if (!items.length) box.innerHTML = '<div class="empty">Nothing awaiting a reply right now — submit more to fill this up. 🚀</div>';
  }

  /* ---------- streak heatmap ---------- */
  function renderHeatmap() {
    var box = $("#heat");
    if (!box) return;
    var cal = DATA.calendar || {};
    var today = new Date(); today.setHours(0, 0, 0, 0);
    var start = new Date(today); start.setDate(start.getDate() - 7 * 13);
    while (start.getDay() !== 0) start.setDate(start.getDate() - 1); // align Sunday
    var max = 0; Object.keys(cal).forEach(function (k) { max = Math.max(max, cal[k]); });
    var d = new Date(start);
    while (d <= today) {
      var k = ymd(d), n = cal[k] || 0;
      var lvl = n === 0 ? "" : n <= 2 ? "l1" : n <= 5 ? "l2" : n <= 10 ? "l3" : "l4";
      var cell = el("div", "cell " + lvl);
      cell.title = k + " · " + n + " application" + (n === 1 ? "" : "s");
      box.appendChild(cell);
      d.setDate(d.getDate() + 1);
    }
  }

  /* ---------- velocity area chart ---------- */
  function renderVelocity() {
    var box = $("#velocity");
    if (!box || !hasApex) return;
    var tl = DATA.timeline || [];
    new ApexCharts(box, {
      chart: { type: "area", height: 150, toolbar: { show: false }, sparkline: { enabled: false }, animations: { enabled: true, speed: 1000 }, fontFamily: "Inter, sans-serif" },
      series: [{ name: "Applications", data: tl.map(function (r) { return r.applied; }) }],
      xaxis: { categories: tl.map(function (r) { return r.day.slice(5); }), labels: { style: { colors: "#868daa", fontSize: "10px" } }, axisBorder: { show: false }, axisTicks: { show: false } },
      yaxis: { labels: { style: { colors: "#868daa" } }, min: 0 },
      colors: ["#c4b1ff"],
      stroke: { curve: "smooth", width: 3 },
      fill: { type: "gradient", gradient: { shadeIntensity: 1, opacityFrom: 0.5, opacityTo: 0.02, stops: [0, 95] } },
      dataLabels: { enabled: false },
      grid: { borderColor: "#232842", strokeDashArray: 4 },
      tooltip: { theme: "dark" }
    }).render();
  }

  /* ---------- hot prospects ---------- */
  function renderProspects() {
    var box = $("#prospects");
    if (!box) return;
    var rows = (DATA.top_chances || []).filter(function (r) { return r.status === "apply_queued" || r.status === "scored"; }).slice(0, 6);
    if (!rows.length) rows = (DATA.top_chances || []).slice(0, 6);
    if (!rows.length) { box.innerHTML = '<div class="empty">No scored prospects in the queue.</div>'; return; }
    rows.forEach(function (r) {
      var p = el("div", "prospect");
      p.innerHTML = '<div class="pscore">' + (r.chance != null ? r.chance + "%" : "—") + "</div>" +
        '<div class="pt"><div class="r">' + esc(r.title || "") + "</div>" +
        '<div class="c">' + esc(r.company || "") + (r.location ? " · " + esc(r.location) : "") + "</div></div>" +
        (r.url ? '<a class="btn" href="' + esc(r.url) + '" target="_blank" rel="noopener"><span class="material-symbols-rounded">open_in_new</span></a>' : "");
      box.appendChild(p);
    });
  }

  /* ---------- milestones + confetti ---------- */
  function renderMilestones() {
    var box = $("#miles");
    if (!box) return;
    var ms = DATA.milestones || [];
    ms.forEach(function (m) {
      var c = el("div", "mile " + (m.achieved ? "done" : "locked"));
      c.innerHTML = '<div class="badge"><span class="material-symbols-rounded">' + esc(m.icon) + "</span></div>" +
        '<div class="ml">' + esc(m.label) + '</div><div class="mh">' + esc(m.hint) + "</div>";
      box.appendChild(c);
    });
    // fire confetti once when a NEW milestone is earned vs last visit
    try {
      var earned = ms.filter(function (m) { return m.achieved; }).map(function (m) { return m.key; });
      var prev = JSON.parse(localStorage.getItem("ja_miles") || "[]");
      var fresh = earned.filter(function (k) { return prev.indexOf(k) < 0; });
      if (fresh.length && prev.length && hasConfetti) {
        setTimeout(function () {
          confetti({ particleCount: 130, spread: 75, origin: { y: 0.6 }, colors: ["#8b5cf6", "#ec4899", "#5ce0a8", "#c4b1ff"] });
        }, 700);
      }
      localStorage.setItem("ja_miles", JSON.stringify(earned));
    } catch (e) {}
  }

  /* ---------- active applications by region ---------- */
  function renderRegions() {
    var box = $("#regionChart");
    if (!box) return;
    var rows = DATA.regions_active || [];
    if (!rows.length) { box.innerHTML = '<div class="empty">No submitted applications yet.</div>'; return; }
    if (!hasApex) {
      box.innerHTML = rows.map(function (r) {
        return '<div class="prospect"><span class="chip">' + esc(r.region) + '</span><div class="pt"><div class="r">' +
          r.sent + " sent · " + r.awaiting + " awaiting · " + r.rejected + " rejected</div></div></div>";
      }).join("");
      return;
    }
    var cats = rows.map(function (r) { return r.region; });
    new ApexCharts(box, {
      chart: { type: "bar", height: Math.max(170, cats.length * 46), stacked: true, toolbar: { show: false }, animations: { enabled: true, speed: 900 }, fontFamily: "Inter, sans-serif" },
      series: [
        { name: "Awaiting", data: rows.map(function (r) { return r.awaiting; }) },
        { name: "Rejected", data: rows.map(function (r) { return r.rejected; }) },
        { name: "Interview", data: rows.map(function (r) { return r.interview; }) }
      ],
      colors: ["#8bb6ff", "#ff8b8b", "#5ce0a8"],
      plotOptions: { bar: { horizontal: true, borderRadius: 5, barHeight: "62%" } },
      xaxis: { categories: cats, labels: { style: { colors: "#868daa" } }, axisBorder: { show: false }, axisTicks: { show: false } },
      yaxis: { labels: { style: { colors: "#b6bcd4", fontWeight: 700, fontSize: "13px" } } },
      legend: { position: "top", horizontalAlign: "left", labels: { colors: "#b6bcd4" }, markers: { radius: 6 } },
      dataLabels: { enabled: true, formatter: function (v) { return v > 0 ? v : ""; }, style: { fontSize: "11px", fontWeight: 700, colors: ["#0c0e16"] } },
      grid: { borderColor: "#232842", strokeDashArray: 4 },
      tooltip: { theme: "dark" }
    }).render();
  }

  /* ---------- daily streak strip (last 7 days) ---------- */
  function renderStreakDays() {
    var box = $("#streakDays");
    if (!box) return;
    var cal = DATA.calendar || {};
    var today = new Date(); today.setHours(0, 0, 0, 0);
    var wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    for (var i = 6; i >= 0; i--) {
      var d = new Date(today); d.setDate(d.getDate() - i);
      var k = ymd(d), n = cal[k] || 0;
      var col = el("div", "sday" + (n > 0 ? " on" : "") + (i === 0 ? " today" : ""));
      col.innerHTML = '<div class="dlabel">' + (i === 0 ? "Today" : wd[d.getDay()]) + "</div>" +
        '<div class="ddot" title="' + k + " · " + n + ' application' + (n === 1 ? "" : "s") + '">' + (n > 0 ? n : "·") + "</div>" +
        '<div class="dnum">' + (d.getMonth() + 1) + "/" + d.getDate() + "</div>";
      box.appendChild(col);
    }
  }

  /* ---------- reveal on scroll ---------- */
  function revealObserver() {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en, i) {
        if (en.isIntersecting) { en.target.style.transitionDelay = (i % 4 * 60) + "ms"; en.target.classList.add("in"); io.unobserve(en.target); }
      });
    }, { threshold: 0.08 });
    $$(".reveal").forEach(function (n) { io.observe(n); });
  }

  /* ---------- functional tabs + filter + auto-refresh ---------- */
  function wireOps() {
    function showTab(name) {
      $$(".tab").forEach(function (t) { t.classList.toggle("active", t.dataset.tab === name); });
      $$(".tabpane").forEach(function (p) { p.classList.toggle("active", p.id === name); });
      try { localStorage.setItem("ja_tab", name); } catch (e) {}
    }
    $$(".tab").forEach(function (t) { t.addEventListener("click", function () { showTab(t.dataset.tab); }); });
    try { var s = localStorage.getItem("ja_tab"); if (s && $("#" + s)) showTab(s); } catch (e) {}

    var search = $("#appSearch");
    window.filterApps = function () {
      var q = (search && search.value || "").toLowerCase();
      var st = ($("#appStatus") && $("#appStatus").value) || "";
      var rg = ($("#appRegion") && $("#appRegion").value) || "";
      $$("#appTable tbody tr").forEach(function (tr) {
        var okText = !q || (tr.dataset.co || "").indexOf(q) >= 0 || (tr.dataset.role || "").indexOf(q) >= 0;
        var okSt = !st || tr.dataset.status === st;
        var okRg = !rg || tr.dataset.region === rg;
        tr.style.display = (okText && okSt && okRg) ? "" : "none";
      });
    };
    [search, $("#appStatus"), $("#appRegion")].forEach(function (n) { if (n) n.addEventListener("input", window.filterApps); });

    setTimeout(function () { if (!document.querySelector("#appSearch:focus")) location.reload(); }, 90000);
  }

  function init() {
    animateCounts(); renderStreakDays(); fillSteps(); renderWaiting(); renderHeatmap(); renderVelocity();
    renderProspects(); renderMilestones(); renderRegions(); revealObserver(); wireOps();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
