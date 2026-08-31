const ratingClass = (rating) => {
  if (!rating) return "";
  return "rating-" + rating.toLowerCase().replace(/\s+/g, "");
};

function fmtPct(v) {
  if (v === null || v === undefined) return "—";
  const cls = v >= 0 ? "pct-pos" : "pct-neg";
  const sign = v >= 0 ? "+" : "";
  return `<span class="${cls}">${sign}${v.toFixed(2)}%</span>`;
}

function sparklineSVG(points, width = 70, height = 22) {
  if (!points || points.length < 2) return "";
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const step = width / (points.length - 1);
  const coords = points.map((p, i) => {
    const x = i * step;
    const y = height - ((p - min) / range) * height;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const up = points[points.length - 1] >= points[0];
  const color = up ? "#35d399" : "#e5484d";
  return `<svg class="spark" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
    <polyline fill="none" stroke="${color}" stroke-width="1.6" points="${coords.join(" ")}" />
  </svg>`;
}

/* ---------------- ticker tape (driven by whichever tab is active) ---------------- */
function renderTape(callSheet) {
  const tape = document.getElementById("tape");
  if (!callSheet || !callSheet.length) {
    tape.innerHTML = `<span class="tape-loading">No data for this view.</span>`;
    return;
  }
  const items = callSheet
    .map(
      (p) => `<span class="tape-item"><b>${p.sector.toUpperCase()}</b> ${p.symbol}
        <span class="tag ${ratingClass(p.rating)}">${p.rating.toUpperCase()}</span>
        &nbsp;${p.pct_5d >= 0 ? "+" : ""}${p.pct_5d.toFixed(1)}% (5d)</span>`
    )
    .join(" &nbsp;·&nbsp; ");
  tape.innerHTML = items + " &nbsp;·&nbsp; " + items;
}

/* ---------------- call sheet cards (generic across all 3 modes) ---------------- */
function renderCallSheet(gridId, callSheet) {
  const grid = document.getElementById(gridId);
  grid.innerHTML = "";
  if (!callSheet || !callSheet.length) {
    grid.innerHTML = `<div class="mode-banner">No data returned — try Refresh.</div>`;
    return;
  }
  callSheet
    .slice()
    .sort((a, b) => b.score - a.score)
    .forEach((p) => {
      const card = document.createElement("div");
      card.className = `cs-card ${ratingClass(p.rating)}`;
      const talkTrack = `${p.sector} — ${p.symbol} (${p.name}): ${p.rating}. ${p.reason}`;
      card.innerHTML = `
        <div class="cs-sector">${p.sector}</div>
        <div class="cs-top-row">
          <div>
            <div class="cs-symbol">${p.symbol}</div>
            <div class="cs-name">${p.name}</div>
          </div>
          <div class="cs-rating ${ratingClass(p.rating)}" style="background:currentColor;">
            <span style="color:#0a0c0f">${p.rating}</span>
          </div>
        </div>
        <div class="cs-price-row">
          <span>Px <b>$${p.price}</b></span>
          <span>5d ${fmtPct(p.pct_5d)}</span>
        </div>
        <div class="cs-reason">${p.reason}</div>
        <div class="cs-footer">
          <span class="cs-score">score ${p.score}/100</span>
          <button class="cs-copy">COPY TALK TRACK</button>
        </div>
      `;
      const btn = card.querySelector(".cs-copy");
      btn.addEventListener("click", () => {
        navigator.clipboard.writeText(talkTrack).then(() => {
          btn.textContent = "COPIED ✓";
          btn.classList.add("copied");
          setTimeout(() => {
            btn.textContent = "COPY TALK TRACK";
            btn.classList.remove("copied");
          }, 1600);
        });
      });
      grid.appendChild(card);
    });
}

/* ---------------- sector / theme accordion (generic) ---------------- */
function tickerRow(row, pickSymbol, mode) {
  const isPick = row.symbol === pickSymbol;
  let col5, col6, col5Label, col6Label;
  if (mode === "penny") {
    if (row.return_1m != null) {
      col5Label = "1M Return"; col5 = fmtPct(row.return_1m);
      col6Label = "3M Return"; col6 = row.return_3m != null ? fmtPct(row.return_3m) : "—";
    } else {
      const ratio = row.averageVolume && row.volume ? (row.volume / row.averageVolume).toFixed(1) + "x" : "—";
      col5Label = "Vol / Avg"; col5 = ratio;
      col6Label = "52W Pos";
      if (row.fiftyTwoWeekHigh && row.fiftyTwoWeekLow && row.fiftyTwoWeekHigh > row.fiftyTwoWeekLow) {
        const pos = ((row.price - row.fiftyTwoWeekLow) / (row.fiftyTwoWeekHigh - row.fiftyTwoWeekLow)) * 100;
        col6 = pos.toFixed(0) + "%";
      } else { col6 = "—"; }
    }
  } else if (mode === "dividend") {
    col5Label = "Yield"; col5 = row.dividendYield != null ? (row.dividendYield * 100).toFixed(1) + "%" : "—";
    col6Label = "Payout"; col6 = row.payoutRatio != null ? (row.payoutRatio * 100).toFixed(0) + "%" : "—";
  } else {
    col5Label = "P/E"; col5 = row.trailingPE ? row.trailingPE.toFixed(1) + "x" : "—";
    col6Label = "ROE"; col6 = row.returnOnEquity != null ? (row.returnOnEquity * 100).toFixed(1) + "%" : "—";
  }
  return { html: `<tr class="${isPick ? "is-pick" : ""}">
    <td><span class="tt-symbol">${row.symbol}</span><br><span class="tt-name">${row.name}</span></td>
    <td>$${row.price}</td>
    <td>${fmtPct(row.pct_5d)}</td>
    <td>${sparklineSVG(row.sparkline)}</td>
    <td>${col5}</td>
    <td>${col6}</td>
    <td><span class="rating-pill ${ratingClass(row.rating)}" style="background:currentColor;">
        <span style="color:#0a0c0f">${row.rating}</span></span></td>
  </tr>`, col5Label, col6Label };
}

function renderAccordion(wrapId, sectors, mode) {
  const wrap = document.getElementById(wrapId);
  wrap.innerHTML = "";
  Object.entries(sectors).forEach(([sectorName, data], idx) => {
    const block = document.createElement("div");
    block.className = "sector-block" + (idx === 0 ? " open" : "");
    const pick = data.pick;

    let subHtml = "";
    Object.entries(data.subindustries).forEach(([subName, rows]) => {
      if (!rows.length) return;
      const rendered = rows.map((r) => tickerRow(r, pick ? pick.symbol : null, mode));
      const col5Label = rendered[0] ? rendered[0].col5Label : "";
      const col6Label = rendered[0] ? rendered[0].col6Label : "";
      subHtml += `<div class="sub-block">
        <div class="sub-title">${subName}</div>
        <table class="tick-table">
          <thead><tr>
            <th>Ticker</th><th>Price</th><th>5D %</th><th>5D Trend</th>
            <th>${col5Label}</th><th>${col6Label}</th><th>Rating</th>
          </tr></thead>
          <tbody>${rendered.map((r) => r.html).join("")}</tbody>
        </table>
      </div>`;
    });

    block.innerHTML = `
      <div class="sector-block-head">
        <div class="sector-block-title">
          <span class="chevron">▶</span>
          <h3>${sectorName}</h3>
        </div>
        <div class="sector-block-pick">
          ${pick ? `Top pick: <b>${pick.symbol}</b> · <span class="${ratingClass(pick.rating)}">${pick.rating}</span>` : "No data"}
        </div>
      </div>
      <div class="sector-block-body"><div style="padding-bottom:4px;">${subHtml}</div></div>
    `;

    const head = block.querySelector(".sector-block-head");
    const body = block.querySelector(".sector-block-body");
    const setHeight = () => {
      body.style.maxHeight = block.classList.contains("open") ? body.scrollHeight + "px" : "0px";
    };
    head.addEventListener("click", () => {
      block.classList.toggle("open");
      setHeight();
    });
    wrap.appendChild(block);
    setHeight();
  });
}

/* ---------------- ETF tables (dividend tab, one per payout frequency) ---------------- */
function renderEtfBucket(containerId, rows) {
  const wrap = document.getElementById(containerId);
  if (!rows || !rows.length) {
    wrap.innerHTML = `<div class="mode-banner">No data — try Refresh.</div>`;
    return;
  }
  const trs = rows
    .map(
      (e) => `<tr>
        <td><span class="tt-symbol">${e.symbol}</span><br><span class="tt-name">${e.name}</span></td>
        <td>$${e.price}</td>
        <td>${fmtPct(e.pct_5d)}</td>
        <td>${sparklineSVG(e.sparkline)}</td>
        <td>${(e.dividendYield || e.fundYield) != null ? ((e.dividendYield || e.fundYield) * 100).toFixed(1) + "%" : "—"}</td>
        <td>${e.expenseRatio != null ? (e.expenseRatio * 100).toFixed(2) + "%" : "—"}</td>
        <td><span class="rating-pill ${ratingClass(e.rating)}" style="background:currentColor;">
            <span style="color:#0a0c0f">${e.rating}</span></span></td>
        <td class="etf-reason">${e.reason}</td>
      </tr>`
    )
    .join("");
  wrap.innerHTML = `<div class="etf-table-outer">
    <table class="tick-table">
      <thead><tr>
        <th>Ticker</th><th>Price</th><th>5D %</th><th>5D Trend</th>
        <th>Yield</th><th>Expense</th><th>Rating</th><th>Why</th>
      </tr></thead>
      <tbody>${trs}</tbody>
    </table>
  </div>`;
}

function renderEtfTables(etfs) {
  renderEtfBucket("etf-table-monthly", etfs.monthly);
  renderEtfBucket("etf-table-quarterly", etfs.quarterly);
  renderEtfBucket("etf-table-annual", etfs.annual);
}

/* ---------------- macro snapshot strip ---------------- */
function renderMacro(macro) {
  const strip = document.getElementById("macro-strip");
  if (!macro || !macro.indicators) {
    strip.innerHTML = "";
    return;
  }
  const items = Object.entries(macro.indicators)
    .map(([label, d]) => `<div class="macro-item"><span>${label}</span> <b>${d.value}</b> ${fmtPct(d.pct_5d)}</div>`)
    .join("");
  strip.innerHTML = items + `<div class="macro-note">${macro.note || ""}</div>`;
}

/* ---------------- ETF families (issuer/theme groups, dividend tab) ---------------- */
function renderEtfFamilies(payload) {
  const wrap = document.getElementById("etf-families-wrap");
  if (!payload || !payload.families) {
    wrap.innerHTML = `<div class="mode-banner">No data — try Refresh.</div>`;
    return;
  }
  wrap.innerHTML = "";
  Object.entries(payload.families).forEach(([family, data]) => {
    const rows = data.rows || [];
    if (!rows.length) return;
    const riskClass = "risk-" + (data.risk_tier || "").toLowerCase().replace(/\s+/g, "");
    const trs = rows
      .map(
        (r) => `<tr>
          <td>#${r.family_rank} <span class="tt-symbol">${r.symbol}</span><br><span class="tt-name">${r.name}</span></td>
          <td>$${r.price}</td>
          <td>${fmtPct(r.pct_5d)}</td>
          <td>${(r.dividendYield || r.fundYield) != null ? ((r.dividendYield || r.fundYield) * 100).toFixed(1) + "%" : "—"}</td>
          <td>${r.return_ytd != null ? fmtPct(r.return_ytd) : "—"}</td>
          <td>${r.return_3m != null ? fmtPct(r.return_3m) : "—"}</td>
          <td>${r.expenseRatio != null ? (r.expenseRatio * 100).toFixed(2) + "%" : "—"}</td>
          <td>${r.totalAssets != null ? "$" + (r.totalAssets / 1e6).toFixed(0) + "M" : "—"}</td>
          <td><span class="rating-pill ${ratingClass(r.rating)}" style="background:currentColor;">
              <span style="color:#0a0c0f">${r.rating}</span></span></td>
          <td class="etf-reason">${r.reason}</td>
        </tr>`
      )
      .join("");
    const block = document.createElement("div");
    block.className = "family-block";
    block.innerHTML = `
      <div class="family-head">
        <div class="family-title">${family}</div>
        <span class="risk-badge ${riskClass}">${data.risk_tier} Risk</span>
      </div>
      <div class="family-table-scroll">
        <table class="tick-table">
          <thead><tr>
            <th>Rank / Ticker</th><th>Price</th><th>5D %</th><th>Yield</th>
            <th>YTD Return</th><th>3M Return</th><th>Expense</th><th>Net Assets</th><th>Rating</th><th>Why</th>
          </tr></thead>
          <tbody>${trs}</tbody>
        </table>
      </div>`;
    wrap.appendChild(block);
  });
}

async function loadEtfFamilies(force = false) {
  try {
    const res = await fetch(force ? "/api/etf-families/refresh" : "/api/etf-families", {
      method: force ? "POST" : "GET",
    });
    const data = await res.json();
    renderEtfFamilies(data);
  } catch (e) {
    console.error(e);
  }
}

/* ---------------- data loading per tab, with lazy caching ---------------- */
const ENDPOINTS = {
  main: { get: "/api/data", refresh: "/api/refresh" },
  penny: { get: "/api/penny", refresh: "/api/penny/refresh" },
  dividend: { get: "/api/dividend", refresh: "/api/dividend/refresh" },
};
const loaded = { main: null, penny: null, dividend: null };
let familiesLoaded = false;
let activeTab = "main";

function renderMode(mode, data) {
  renderCallSheet(`callsheet-grid-${mode}`, data.call_sheet);
  renderAccordion(`sector-accordion-${mode}`, data.sectors, mode);
  if (mode === "dividend") renderEtfTables(data.etfs);
  if (data.macro) renderMacro(data.macro);
}

async function loadTab(mode, force = false) {
  const btn = document.getElementById("refresh-btn");
  btn.classList.add("loading");
  try {
    const ep = ENDPOINTS[mode];
    const res = await fetch(force ? ep.refresh : ep.get, { method: force ? "POST" : "GET" });
    const data = await res.json();
    loaded[mode] = data;
    renderMode(mode, data);
    if (mode === "dividend" && (force || !familiesLoaded)) {
      familiesLoaded = true;
      loadEtfFamilies(force);
    }
    if (mode === activeTab) {
      renderTape(data.call_sheet);
      document.getElementById("last-updated").textContent =
        "Updated " + new Date(data.generated_at * 1000).toLocaleTimeString();
    }
  } catch (e) {
    document.getElementById("last-updated").textContent = "Error loading data";
    console.error(e);
  } finally {
    btn.classList.remove("loading");
  }
}

function switchTab(mode) {
  activeTab = mode;
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === mode));
  document.querySelectorAll(".panel-view").forEach((p) => p.classList.toggle("active", p.dataset.panel === mode));
  if (loaded[mode]) {
    renderTape(loaded[mode].call_sheet);
    document.getElementById("last-updated").textContent =
      "Updated " + new Date(loaded[mode].generated_at * 1000).toLocaleTimeString();
    if (mode === "dividend" && !familiesLoaded) {
      familiesLoaded = true;
      loadEtfFamilies(false);
    }
  } else {
    loadTab(mode, false);
  }
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

document.getElementById("refresh-btn").addEventListener("click", () => loadTab(activeTab, true));

document.getElementById("rescan-btn").addEventListener("click", async () => {
  const btn = document.getElementById("rescan-btn");
  btn.classList.add("loading");
  try {
    const res = await fetch("/api/rescan", { method: "POST" });
    const all = await res.json();
    loaded.main = all.main;
    loaded.penny = all.penny;
    loaded.dividend = all.dividend;
    renderMode("main", all.main);
    renderMode("penny", all.penny);
    renderMode("dividend", all.dividend);
    familiesLoaded = true;
    loadEtfFamilies(true);
    switchTab(activeTab);
  } catch (e) {
    console.error(e);
  } finally {
    btn.classList.remove("loading");
  }
});

async function loadMacro() {
  try {
    const res = await fetch("/api/macro");
    const macro = await res.json();
    renderMacro(macro);
  } catch (e) {
    console.error(e);
  }
}

loadTab("main", false);
loadMacro();

if (typeof initAutoRefresh === "function") {
  initAutoRefresh("auto-refresh-main", () => loadTab(activeTab, true), "stockTerminal.autoRefresh.main");
}
