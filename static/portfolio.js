function pfFmtPct(v) {
  if (v === null || v === undefined) return "—";
  const cls = v >= 0 ? "pct-pos" : "pct-neg";
  const sign = v >= 0 ? "+" : "";
  return `<span class="${cls}">${sign}${v.toFixed(2)}%</span>`;
}
function pfFmtMoney(v) {
  if (v === null || v === undefined) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function pfFmtGain(v) {
  if (v === null || v === undefined) return "—";
  const cls = v >= 0 ? "pct-pos" : "pct-neg";
  const sign = v >= 0 ? "+" : "-";
  return `<span class="${cls}">${sign}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>`;
}
function pfTimeAgo(tsOrIso) {
  if (!tsOrIso) return "";
  let ms;
  if (typeof tsOrIso === "number") {
    ms = tsOrIso > 2e10 ? tsOrIso : tsOrIso * 1000; // handle sec vs ms epoch
  } else {
    const parsed = Date.parse(tsOrIso);
    if (isNaN(parsed)) return "";
    ms = parsed;
  }
  const diffH = (Date.now() - ms) / 3.6e6;
  if (diffH < 1) return Math.max(1, Math.round(diffH * 60)) + "m ago";
  if (diffH < 24) return Math.round(diffH) + "h ago";
  return Math.round(diffH / 24) + "d ago";
}

const SECTOR_COLORS = ["#4fc3f7", "#35d399", "#e8b94d", "#e5484d", "#8fd858", "#c792ea", "#f78166", "#79c0ff", "#d2a8ff", "#ffa657"];

let pfData = null;
let pfFilter = "open";
let pfPortfolios = [];
let currentPortfolioId = null;
const LAST_PORTFOLIO_KEY = "stockTerminal.lastPortfolioId";

/* ---------------- portfolio selector ---------------- */
async function loadPortfolioList(selectId = null) {
  const res = await fetch("/api/portfolios");
  pfPortfolios = await res.json();
  const select = document.getElementById("pf-portfolio-select");
  select.innerHTML = pfPortfolios
    .map((p) => `<option value="${p.id}">${p.name} (${p.position_count})</option>`)
    .join("");

  const remembered = selectId || parseInt(localStorage.getItem(LAST_PORTFOLIO_KEY), 10);
  const validIds = pfPortfolios.map((p) => p.id);
  currentPortfolioId = validIds.includes(remembered) ? remembered : (validIds[0] || null);
  if (currentPortfolioId) {
    select.value = currentPortfolioId;
    localStorage.setItem(LAST_PORTFOLIO_KEY, currentPortfolioId);
  }
}

document.getElementById("pf-portfolio-select").addEventListener("change", (e) => {
  currentPortfolioId = parseInt(e.target.value, 10);
  localStorage.setItem(LAST_PORTFOLIO_KEY, currentPortfolioId);
  loadPortfolio(false);
  loadNews(false);
});

/* ---------------- new / rename portfolio modal ---------------- */
let nameModalMode = "new"; // or "rename"

function openNameModal(mode) {
  nameModalMode = mode;
  document.getElementById("pf-name-error").textContent = "";
  const input = document.getElementById("pf-name-input");
  if (mode === "rename") {
    document.getElementById("pf-name-modal-title").textContent = "Rename Portfolio";
    const current = pfPortfolios.find((p) => p.id === currentPortfolioId);
    input.value = current ? current.name : "";
  } else {
    document.getElementById("pf-name-modal-title").textContent = "New Portfolio";
    input.value = "";
  }
  document.getElementById("pf-name-modal-backdrop").classList.add("open");
  setTimeout(() => input.focus(), 50);
}
function closeNameModal() {
  document.getElementById("pf-name-modal-backdrop").classList.remove("open");
}
document.getElementById("pf-name-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "pf-name-modal-backdrop") closeNameModal();
});
document.getElementById("pf-new-btn").addEventListener("click", () => openNameModal("new"));
document.getElementById("pf-rename-btn").addEventListener("click", () => openNameModal("rename"));

document.getElementById("pf-name-modal-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = document.getElementById("pf-name-input").value.trim();
  const errEl = document.getElementById("pf-name-error");
  if (!name) {
    errEl.textContent = "Name can't be empty.";
    return;
  }
  try {
    if (nameModalMode === "new") {
      const res = await fetch("/api/portfolios", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
      });
      const created = await res.json();
      if (!res.ok) { errEl.textContent = created.error || "Couldn't create portfolio."; return; }
      await loadPortfolioList(created.id);
    } else {
      const res = await fetch(`/api/portfolios/${currentPortfolioId}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
      });
      if (!res.ok) { errEl.textContent = (await res.json()).error || "Couldn't rename portfolio."; return; }
      await loadPortfolioList(currentPortfolioId);
    }
    closeNameModal();
    loadPortfolio(false);
    loadNews(false);
  } catch (err) {
    errEl.textContent = "Network error — please try again.";
    console.error(err);
  }
});

/* ---------------- toolbar actions: clear / delete / clear-history / export ---------------- */
document.getElementById("pf-clear-btn").addEventListener("click", async () => {
  const current = pfPortfolios.find((p) => p.id === currentPortfolioId);
  const label = current ? current.name : "this portfolio";
  if (!confirm(`Clear ALL positions in "${label}"? The portfolio itself stays, but every position in it will be permanently deleted. This can't be undone.`)) return;
  await fetch(`/api/portfolios/${currentPortfolioId}/clear`, { method: "POST" });
  await loadPortfolioList(currentPortfolioId);
  loadPortfolio(false);
  loadNews(false);
});

document.getElementById("pf-delete-btn").addEventListener("click", async () => {
  const current = pfPortfolios.find((p) => p.id === currentPortfolioId);
  const label = current ? current.name : "this portfolio";
  if (pfPortfolios.length === 1) {
    if (!confirm(`"${label}" is your only portfolio. Deleting it will remove all its positions and immediately create a fresh empty "My Portfolio" in its place. Continue?`)) return;
  } else {
    if (!confirm(`Delete the portfolio "${label}" and all ${current ? current.position_count : ""} of its positions? This can't be undone.`)) return;
  }
  await fetch(`/api/portfolios/${currentPortfolioId}`, { method: "DELETE" });
  await loadPortfolioList();
  loadPortfolio(false);
  loadNews(false);
});

document.getElementById("pf-clear-history-btn").addEventListener("click", async () => {
  const totalPositions = pfPortfolios.reduce((sum, p) => sum + p.position_count, 0);
  const confirmText = `RESET ALL DATA — this deletes EVERY portfolio (${pfPortfolios.length}) and EVERY position (${totalPositions}) in the entire app, with no way to undo it. Type "DELETE" to confirm.`;
  const typed = prompt(confirmText);
  if (typed !== "DELETE") return;
  await fetch("/api/portfolios/clear-all", { method: "POST" });
  await loadPortfolioList();
  loadPortfolio(false);
  loadNews(false);
});

document.getElementById("pf-export-btn").addEventListener("click", () => {
  if (!pfData || !pfData.positions.length) {
    alert("No positions to export in this portfolio.");
    return;
  }
  const headers = [
    "Symbol", "Name", "Quantity", "Buy Price", "Buy Date", "Current/Sell Price", "Sell Date",
    "Cost Basis", "Status", "Realized Gain", "Realized Gain %", "Unrealized Gain", "Unrealized Gain %",
    "Annualized Return %", "Notes",
  ];
  const rows = pfData.positions.map((p) => [
    p.symbol, p.name || "", p.quantity, p.buy_price, p.buy_date,
    p.is_closed ? p.sell_price : p.current_price, p.sell_date || "",
    p.cost_basis, p.is_closed ? "Closed" : "Open",
    p.realized_gain ?? "", p.realized_gain_pct ?? "",
    p.unrealized_gain ?? "", p.unrealized_gain_pct ?? "",
    p.annualized_return_pct ?? "", (p.notes || "").replace(/"/g, '""'),
  ]);
  const csv = [headers, ...rows]
    .map((row) => row.map((cell) => (typeof cell === "string" && cell.includes(",") ? `"${cell}"` : cell)).join(","))
    .join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const portfolioName = (pfData.portfolio && pfData.portfolio.name) || "portfolio";
  a.href = url;
  a.download = `${portfolioName.replace(/[^a-z0-9]+/gi, "_")}_${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
});

/* ---------------- summary / sector allocation ---------------- */
function renderSummary(summary) {
  const grid = document.getElementById("pf-summary-grid");
  const cards = [
    {
      label: "Open Positions Value", valueHtml: pfFmtMoney(summary.total_value_open),
      sub: `${summary.open_count} open · cost ${pfFmtMoney(summary.total_cost_open)}`,
    },
    {
      label: "Unrealized Gain", valueHtml: pfFmtGain(summary.total_unrealized_gain),
      sub: summary.total_unrealized_gain_pct != null ? pfFmtPct(summary.total_unrealized_gain_pct) + " on open cost" : "—",
    },
    {
      label: "Realized Gain", valueHtml: pfFmtGain(summary.total_realized_gain),
      sub: `${summary.closed_count} closed` + (summary.total_realized_gain_pct != null ? " · " + pfFmtPct(summary.total_realized_gain_pct) : ""),
    },
    {
      label: "Portfolio XIRR",
      valueHtml: summary.portfolio_xirr_pct != null ? pfFmtPct(summary.portfolio_xirr_pct) : "<span style=\"color:var(--dim)\">n/a</span>",
      sub: "Money-weighted, annualized, real cash-flow dates",
    },
  ];
  grid.innerHTML = cards
    .map((c) => `<div class="pf-card">
        <div class="pf-card-label">${c.label}</div>
        <div class="pf-card-value">${c.valueHtml}</div>
        <div class="pf-card-sub">${c.sub}</div>
      </div>`)
    .join("");
  renderSectorAllocation(summary.sector_allocation, summary.total_value_open);
}

function renderSectorAllocation(alloc, total) {
  const wrap = document.getElementById("pf-sector-alloc");
  const entries = Object.entries(alloc || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length || !total) {
    wrap.innerHTML = "";
    return;
  }
  const segs = entries.map(([sector, value], i) => ({
    sector, pct: (value / total) * 100, color: SECTOR_COLORS[i % SECTOR_COLORS.length],
  }));
  wrap.innerHTML = `
    <div class="pf-sector-alloc-title">Sector Allocation (Open Positions)</div>
    <div class="pf-alloc-bar">
      ${segs.map((s) => `<div class="pf-alloc-seg" style="width:${s.pct}%;background:${s.color}" title="${s.sector}: ${s.pct.toFixed(1)}%"></div>`).join("")}
    </div>
    <div class="pf-alloc-legend">
      ${segs.map((s) => `<span><span class="swatch" style="background:${s.color}"></span>${s.sector} — ${s.pct.toFixed(1)}%</span>`).join("")}
    </div>`;
}

/* ---------------- positions table ---------------- */
function positionRow(p) {
  const isClosed = p.is_closed;
  const priceCol = isClosed
    ? `$${p.sell_price != null ? p.sell_price.toFixed(2) : "—"}`
    : `$${p.current_price != null ? p.current_price.toFixed(2) : "—"}`;
  const dateCol = isClosed ? p.sell_date : "—";
  const gainVal = isClosed ? p.realized_gain : p.unrealized_gain;
  const gainPct = isClosed ? p.realized_gain_pct : p.unrealized_gain_pct;
  const gainLabel = isClosed ? "Realized" : "Unrealized";
  const annFlag = p.annualized_return_pct != null && p.holding_days < 30
    ? `<span class="pf-annualized-flag" title="Short holding period (${p.holding_days}d) — this is a mathematical extrapolation, not a realistic expectation.">*</span>`
    : "";

  return `<tr>
    <td><span class="tt-symbol">${p.symbol}</span><br><span class="tt-name">${p.name || ""}</span>${isClosed ? '<span class="pf-closed-tag">CLOSED</span>' : ""}</td>
    <td>${p.quantity}</td>
    <td>$${p.buy_price.toFixed(2)}</td>
    <td>${p.buy_date}</td>
    <td>${priceCol}</td>
    <td>${dateCol}</td>
    <td>${pfFmtMoney(p.cost_basis)}</td>
    <td>${gainLabel}: ${pfFmtGain(gainVal)}${annFlag}</td>
    <td>${gainPct != null ? pfFmtPct(gainPct) : "—"}</td>
    <td>${p.annualized_return_pct != null ? pfFmtPct(p.annualized_return_pct) : "—"}</td>
    <td>${p.allocation_pct != null ? p.allocation_pct.toFixed(1) + "%" : "—"}</td>
    <td class="pf-row-actions">
      <button class="pf-icon-btn" onclick="openPositionModal(${p.id})">Edit</button>
      <button class="pf-icon-btn" onclick="viewQuote('${p.symbol}')">Quote</button>
      <button class="pf-icon-btn danger" onclick="deletePosition(${p.id}, '${p.symbol}')">Delete</button>
    </td>
  </tr>`;
}

function renderTable() {
  const wrap = document.getElementById("pf-table-wrap");
  if (!pfData) return;
  let rows = pfData.positions;
  if (pfFilter === "open") rows = rows.filter((p) => !p.is_closed);
  else if (pfFilter === "closed") rows = rows.filter((p) => p.is_closed);

  if (!rows.length) {
    wrap.innerHTML = `<div class="pf-empty-state">No ${pfFilter === "all" ? "" : pfFilter} positions yet. Click "+ Add Position" to start tracking one.</div>`;
    return;
  }
  wrap.innerHTML = `<div class="pf-table-outer">
    <table class="tick-table">
      <thead><tr>
        <th>Ticker</th><th>Qty</th><th>Buy Price</th><th>Buy Date</th>
        <th>Current / Sell Price</th><th>Sell Date</th><th>Cost Basis</th>
        <th>Gain ($)</th><th>Gain (%)</th><th>Annualized</th><th>Allocation</th><th>Actions</th>
      </tr></thead>
      <tbody>${rows.map(positionRow).join("")}</tbody>
    </table>
  </div>`;
}

document.querySelectorAll(".pf-filter-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    pfFilter = btn.dataset.filter;
    document.querySelectorAll(".pf-filter-btn").forEach((b) => b.classList.toggle("active", b === btn));
    renderTable();
  });
});

/* ---------------- news ---------------- */
function renderNews(newsBySymbol) {
  const wrap = document.getElementById("pf-news-wrap");
  const symbols = Object.keys(newsBySymbol || {});
  if (!symbols.length) {
    wrap.innerHTML = `<div class="pf-empty-state">No holdings yet — add a position to see news for it here.</div>`;
    return;
  }
  const anyNews = symbols.some((s) => (newsBySymbol[s] || []).length);
  if (!anyNews) {
    wrap.innerHTML = `<div class="pf-empty-state">No recent headlines found for your current holdings.</div>`;
    return;
  }
  wrap.innerHTML = symbols
    .map((sym) => {
      const items = newsBySymbol[sym] || [];
      if (!items.length) return "";
      return `<div class="pf-news-group">
        <div class="pf-news-symbol">${sym}</div>
        ${items
          .map(
            (n) => `<div class="pf-news-item">
              <a class="pf-news-title" href="${n.link || "#"}" target="_blank" rel="noopener">${n.title}</a>
              <div class="pf-news-meta">${n.publisher || ""}${n.published ? " · " + pfTimeAgo(n.published) : ""}</div>
            </div>`
          )
          .join("")}
      </div>`;
    })
    .join("");
}

async function loadNews(force = false) {
  if (!currentPortfolioId) return;
  try {
    const res = await fetch(`/api/portfolios/${currentPortfolioId}/news`);
    const news = await res.json();
    renderNews(news);
  } catch (e) {
    console.error(e);
  }
}

/* ---------------- main load / refresh ---------------- */
async function loadPortfolio(force = false) {
  if (!currentPortfolioId) return;
  const btn = document.getElementById("pf-refresh-btn");
  btn.classList.add("loading");
  try {
    const res = await fetch(
      force ? `/api/portfolios/${currentPortfolioId}/refresh` : `/api/portfolios/${currentPortfolioId}`,
      { method: force ? "POST" : "GET" }
    );
    if (!res.ok) {
      document.getElementById("pf-table-wrap").innerHTML = `<div class="pf-empty-state">Couldn't load this portfolio.</div>`;
      return;
    }
    pfData = await res.json();
    renderSummary(pfData.summary);
    renderTable();
    document.getElementById("last-updated").textContent = "Updated " + new Date(pfData.generated_at * 1000).toLocaleTimeString();
  } catch (e) {
    console.error(e);
  } finally {
    btn.classList.remove("loading");
  }
}

document.getElementById("pf-refresh-btn").addEventListener("click", () => {
  loadPortfolio(true);
  loadNews(true);
});

/* ---------------- add / edit position modal ---------------- */
function openPositionModal(id) {
  const backdrop = document.getElementById("pf-modal-backdrop");
  const form = document.getElementById("pf-modal-form");
  form.reset();
  document.getElementById("pf-form-error").textContent = "";
  document.getElementById("pf-form-id").value = "";

  if (id) {
    const p = pfData.positions.find((x) => x.id === id);
    document.getElementById("pf-modal-title").textContent = `Edit ${p.symbol}`;
    document.getElementById("pf-form-id").value = p.id;
    document.getElementById("pf-form-symbol").value = p.symbol;
    document.getElementById("pf-form-symbol").disabled = true;
    document.getElementById("pf-form-quantity").value = p.quantity;
    document.getElementById("pf-form-buy-price").value = p.buy_price;
    document.getElementById("pf-form-buy-date").value = p.buy_date;
    document.getElementById("pf-form-sell-price").value = p.sell_price || "";
    document.getElementById("pf-form-sell-date").value = p.sell_date || "";
    document.getElementById("pf-form-notes").value = p.notes || "";
  } else {
    document.getElementById("pf-modal-title").textContent = "Add Position";
    document.getElementById("pf-form-symbol").disabled = false;
    document.getElementById("pf-form-buy-date").value = new Date().toISOString().slice(0, 10);
  }
  backdrop.classList.add("open");
}
function closePositionModal() {
  document.getElementById("pf-modal-backdrop").classList.remove("open");
}
document.getElementById("pf-add-btn").addEventListener("click", () => openPositionModal(null));
document.getElementById("pf-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "pf-modal-backdrop") closePositionModal();
});

async function deletePosition(id, symbol) {
  if (!confirm(`Delete position in ${symbol}? This can't be undone.`)) return;
  try {
    const res = await fetch(`/api/portfolios/${currentPortfolioId}/positions/${id}`, { method: "DELETE" });
    pfData = await res.json();
    renderSummary(pfData.summary);
    renderTable();
    await loadPortfolioList(currentPortfolioId);
    loadNews(false);
  } catch (e) {
    console.error(e);
  }
}

function viewQuote(symbol) {
  renderQuoteModal(symbol);
}

document.getElementById("pf-modal-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errEl = document.getElementById("pf-form-error");
  errEl.textContent = "";

  const id = document.getElementById("pf-form-id").value;
  const symbol = document.getElementById("pf-form-symbol").value.trim().toUpperCase();
  const quantity = parseFloat(document.getElementById("pf-form-quantity").value);
  const buyPrice = parseFloat(document.getElementById("pf-form-buy-price").value);
  const buyDate = document.getElementById("pf-form-buy-date").value;
  const sellPriceRaw = document.getElementById("pf-form-sell-price").value;
  const sellDateRaw = document.getElementById("pf-form-sell-date").value;
  const notes = document.getElementById("pf-form-notes").value;

  if (!symbol || !quantity || quantity <= 0 || !buyPrice || buyPrice <= 0 || !buyDate) {
    errEl.textContent = "Symbol, quantity, buy price, and buy date are all required.";
    return;
  }
  if ((sellPriceRaw && !sellDateRaw) || (!sellPriceRaw && sellDateRaw)) {
    errEl.textContent = "Sell price and sell date must be filled in together (or both left blank).";
    return;
  }
  if (sellDateRaw && sellDateRaw < buyDate) {
    errEl.textContent = "Sell date can't be before the buy date.";
    return;
  }

  const body = {
    symbol, quantity, buy_price: buyPrice, buy_date: buyDate,
    sell_price: sellPriceRaw ? parseFloat(sellPriceRaw) : null,
    sell_date: sellDateRaw || null,
    notes: notes || null,
  };

  try {
    const url = id
      ? `/api/portfolios/${currentPortfolioId}/positions/${id}`
      : `/api/portfolios/${currentPortfolioId}/positions`;
    const res = await fetch(url, {
      method: id ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json();
      errEl.textContent = err.error || "Something went wrong saving this position.";
      return;
    }
    pfData = await res.json();
    renderSummary(pfData.summary);
    renderTable();
    closePositionModal();
    await loadPortfolioList(currentPortfolioId);
    loadNews(false);
  } catch (e) {
    errEl.textContent = "Network error — please try again.";
    console.error(e);
  }
});

/* ---------------- init ---------------- */
(async function initPortfolioPage() {
  await loadPortfolioList();
  await loadPortfolio(false);
  await loadNews(false);

  if (typeof initAutoRefresh === "function") {
    initAutoRefresh("auto-refresh-portfolio", () => loadPortfolio(true), "stockTerminal.autoRefresh.portfolio");
  }
})();
