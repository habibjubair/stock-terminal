/* Shared across index.html and portfolio.html: a global ticker/company
   search bar with live suggestions, opening a full fundamental + technical
   detail modal on selection. */

let _searchDebounce = null;
let _searchResults = [];
let _searchActiveIndex = -1;

function _fmtBig(n) {
  if (n == null) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e12) return "$" + (n / 1e12).toFixed(2) + "T";
  if (abs >= 1e9) return "$" + (n / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return "$" + (n / 1e6).toFixed(1) + "M";
  return "$" + n.toLocaleString();
}

function _fmtPctSearch(v) {
  if (v === null || v === undefined) return "—";
  const cls = v >= 0 ? "pct-pos" : "pct-neg";
  const sign = v >= 0 ? "+" : "";
  return `<span class="${cls}">${sign}${v.toFixed(2)}%</span>`;
}

function _sparkSVG(points, width = 260, height = 60) {
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
  return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
    <polyline fill="none" stroke="${color}" stroke-width="2" points="${coords.join(" ")}" />
  </svg>`;
}

async function renderQuoteModal(symbol) {
  const backdrop = document.getElementById("quote-modal-backdrop");
  const card = document.getElementById("quote-modal-card");
  card.innerHTML = `<div class="modal-loading">Loading ${symbol}…</div>`;
  backdrop.classList.add("open");

  try {
    const res = await fetch(`/api/quote/${encodeURIComponent(symbol)}`);
    if (!res.ok) {
      card.innerHTML = `<div class="modal-loading">No data found for "${symbol}".</div>
        <button class="modal-close-btn" onclick="closeQuoteModal()">Close</button>`;
      return;
    }
    const d = await res.json();
    const changeUp = d.pct_5d >= 0;

    card.innerHTML = `
      <button class="modal-close-x" onclick="closeQuoteModal()">✕</button>
      <div class="modal-header">
        <div>
          <div class="modal-symbol">${d.symbol}</div>
          <div class="modal-name">${d.name}${d.companySector ? " · " + d.companySector : ""}${d.companyIndustry ? " / " + d.companyIndustry : ""}</div>
        </div>
        <div class="modal-price-block">
          <div class="modal-price">$${d.price}</div>
          <div class="modal-price-chg">${_fmtPctSearch(d.pct_5d)} (5d)</div>
        </div>
      </div>
      <div class="modal-spark">${_sparkSVG(d.sparkline)}</div>

      <div class="modal-grid">
        <div class="modal-section">
          <div class="modal-section-title">Price &amp; Technical</div>
          <div class="modal-row"><span>1-Month Return</span><b>${d.return_1m != null ? _fmtPctSearch(d.return_1m) : "—"}</b></div>
          <div class="modal-row"><span>3-Month Return</span><b>${d.return_3m != null ? _fmtPctSearch(d.return_3m) : "—"}</b></div>
          <div class="modal-row"><span>YTD Return</span><b>${d.return_ytd != null ? _fmtPctSearch(d.return_ytd) : "—"}</b></div>
          <div class="modal-row"><span>52-Week Range</span><b>${d.fiftyTwoWeekLow != null ? "$" + d.fiftyTwoWeekLow : "—"} – ${d.fiftyTwoWeekHigh != null ? "$" + d.fiftyTwoWeekHigh : "—"}</b></div>
          <div class="modal-row"><span>Beta</span><b>${d.beta != null ? d.beta.toFixed(2) : "—"}</b></div>
          <div class="modal-row"><span>Avg Volume</span><b>${d.averageVolume != null ? d.averageVolume.toLocaleString() : "—"}</b></div>
        </div>

        <div class="modal-section">
          <div class="modal-section-title">Fundamentals</div>
          <div class="modal-row"><span>P/E (Trailing)</span><b>${d.trailingPE != null ? d.trailingPE.toFixed(1) + "x" : "—"}</b></div>
          <div class="modal-row"><span>P/E (Forward)</span><b>${d.forwardPE != null ? d.forwardPE.toFixed(1) + "x" : "—"}</b></div>
          <div class="modal-row"><span>Revenue Growth</span><b>${d.revenueGrowth != null ? _fmtPctSearch(d.revenueGrowth * 100) : "—"}</b></div>
          <div class="modal-row"><span>Earnings Growth</span><b>${d.earningsGrowth != null ? _fmtPctSearch(d.earningsGrowth * 100) : "—"}</b></div>
          <div class="modal-row"><span>Profit Margin</span><b>${d.profitMargins != null ? (d.profitMargins * 100).toFixed(1) + "%" : "—"}</b></div>
          <div class="modal-row"><span>ROE</span><b>${d.returnOnEquity != null ? (d.returnOnEquity * 100).toFixed(1) + "%" : "—"}</b></div>
          <div class="modal-row"><span>Current Ratio</span><b>${d.currentRatio != null ? d.currentRatio.toFixed(2) : "—"}</b></div>
          <div class="modal-row"><span>Market Cap</span><b>${_fmtBig(d.marketCap)}</b></div>
        </div>

        <div class="modal-section">
          <div class="modal-section-title">Dividend &amp; Analyst</div>
          <div class="modal-row"><span>Dividend Yield</span><b>${d.dividendYield != null ? (d.dividendYield * 100).toFixed(2) + "%" : "—"}</b></div>
          <div class="modal-row"><span>Payout Ratio</span><b>${d.payoutRatio != null ? (d.payoutRatio * 100).toFixed(0) + "%" : "—"}</b></div>
          <div class="modal-row"><span>Analyst Consensus</span><b>${d.recommendationMean != null ? d.recommendationMean.toFixed(1) + "/5" : "—"}</b></div>
          <div class="modal-row"><span>Analyst Target</span><b>${d.targetMeanPrice != null ? "$" + d.targetMeanPrice.toFixed(2) : "—"}</b></div>
        </div>
      </div>
      <div class="modal-footer-note">Composite screening data — not personalized investment advice.</div>
    `;
  } catch (e) {
    card.innerHTML = `<div class="modal-loading">Error loading ${symbol}.</div>
      <button class="modal-close-btn" onclick="closeQuoteModal()">Close</button>`;
    console.error(e);
  }
}

function closeQuoteModal() {
  document.getElementById("quote-modal-backdrop").classList.remove("open");
}

function _renderSuggestions(results) {
  const box = document.getElementById("search-suggestions");
  _searchResults = results;
  _searchActiveIndex = -1;
  if (!results.length) {
    box.innerHTML = "";
    box.classList.remove("open");
    return;
  }
  box.innerHTML = results
    .map(
      (r, i) => `<div class="search-suggestion" data-index="${i}" data-symbol="${r.symbol}">
        <span class="ss-symbol">${r.symbol}</span>
        <span class="ss-name">${r.name}</span>
        <span class="ss-meta">${r.exchange || ""} ${r.type ? "· " + r.type : ""}</span>
      </div>`
    )
    .join("");
  box.classList.add("open");
  box.querySelectorAll(".search-suggestion").forEach((el) => {
    el.addEventListener("click", () => {
      renderQuoteModal(el.dataset.symbol);
      box.classList.remove("open");
      document.getElementById("global-search-input").value = "";
    });
  });
}

function initSearchBar() {
  const input = document.getElementById("global-search-input");
  const box = document.getElementById("search-suggestions");
  if (!input) return;

  input.addEventListener("input", () => {
    const q = input.value.trim();
    clearTimeout(_searchDebounce);
    if (q.length < 1) {
      box.classList.remove("open");
      return;
    }
    _searchDebounce = setTimeout(async () => {
      try {
        const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
        const results = await res.json();
        _renderSuggestions(results);
      } catch (e) {
        console.error(e);
      }
    }, 250);
  });

  input.addEventListener("keydown", (e) => {
    const items = box.querySelectorAll(".search-suggestion");
    if (e.key === "ArrowDown") {
      e.preventDefault();
      _searchActiveIndex = Math.min(_searchActiveIndex + 1, items.length - 1);
      items.forEach((it, i) => it.classList.toggle("active", i === _searchActiveIndex));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      _searchActiveIndex = Math.max(_searchActiveIndex - 1, 0);
      items.forEach((it, i) => it.classList.toggle("active", i === _searchActiveIndex));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const target = _searchActiveIndex >= 0 ? _searchResults[_searchActiveIndex] : _searchResults[0];
      if (target) {
        renderQuoteModal(target.symbol);
        box.classList.remove("open");
        input.value = "";
      }
    } else if (e.key === "Escape") {
      box.classList.remove("open");
    }
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search-wrap")) box.classList.remove("open");
  });

  document.getElementById("quote-modal-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "quote-modal-backdrop") closeQuoteModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeQuoteModal();
  });
}

document.addEventListener("DOMContentLoaded", initSearchBar);
