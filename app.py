"""
Sector Call Sheet — a small web terminal for finding the strongest fundamental
stock in each of the 11 GICS sectors, built on Yahoo Finance data (yfinance).

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000
"""

from flask import Flask, jsonify, render_template, request
import yfinance as yf
import numpy as np
import pandas as pd
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

# ---------------------------------------------------------------------------
# 1. DYNAMIC UNIVERSE DISCOVERY
#    Nothing below is a fixed stock list. Every cycle (re-discovered daily),
#    the app asks Yahoo Finance directly: "what are today's top companies in
#    each sector/industry", "what's actually moving under $5 right now",
#    "what's paying a real dividend right now", "what ETFs actually qualify
#    as high-yield right now". The only fixed things are the QUESTIONS
#    (sector keys, screening thresholds) — never the resulting tickers.
#
#    The *_FALLBACK dicts below are a safety net only — used if a live Yahoo
#    screener/sector call fails (rate limit, schema change, no network) so
#    the app degrades gracefully instead of showing an empty page. In normal
#    operation they are never used.
# ---------------------------------------------------------------------------

# Yahoo's own 11 sector keys (yf.Sector) map 1:1 onto GICS's 11 sectors.
SECTOR_KEYS = {
    "Energy": "energy",
    "Materials": "basic-materials",
    "Industrials": "industrials",
    "Consumer Discretionary": "consumer-cyclical",
    "Consumer Staples": "consumer-defensive",
    "Health Care": "healthcare",
    "Financials": "financial-services",
    "Information Technology": "technology",
    "Communication Services": "communication-services",
    "Utilities": "utilities",
    "Real Estate": "real-estate",
}

# Yahoo's exact sector label strings, as required by the EquityQuery screener
# (used for the dividend-stock screen, which filters by sector directly).
SCREENER_SECTOR_NAMES = {
    "Energy": "Energy",
    "Consumer Staples": "Consumer Defensive",
    "Health Care": "Healthcare",
    "Utilities": "Utilities",
    "Financials": "Financial Services",
    "Communication Services": "Communication Services",
    "Real Estate (REITs)": "Real Estate",
    "Industrials": "Industrials",
}

# Major US exchanges only — deliberately excludes OTC/Pink (PNK) for quality,
# especially important for the penny-stock screen.
MAIN_US_EXCHANGES = ["NMS", "NYQ", "NGM", "NCM", "ASE"]

SECTORS_FALLBACK = {
    "Energy": {"Oil & Gas E&P": ["XOM", "CVX", "COP", "EOG", "OXY"], "Oil & Gas Equipment & Services": ["SLB", "HAL", "BKR"]},
    "Materials": {"Chemicals": ["LIN", "APD", "SHW", "DD"]},
    "Industrials": {"Aerospace & Defense": ["BA", "LMT", "RTX", "NOC", "GD"]},
    "Consumer Discretionary": {"Broadline / Specialty Retail": ["AMZN", "HD", "LOW", "TJX"]},
    "Consumer Staples": {"Food & Staples Retailing": ["WMT", "COST", "KR"]},
    "Health Care": {"Pharmaceuticals": ["LLY", "JNJ", "PFE", "MRK"]},
    "Financials": {"Banks": ["JPM", "BAC", "WFC", "C"]},
    "Information Technology": {"Semiconductors": ["NVDA", "AVGO", "AMD", "TXN", "INTC"]},
    "Communication Services": {"Interactive Media & Services": ["GOOGL", "META"]},
    "Utilities": {"Electric Utilities": ["NEE", "DUK", "SO", "D"]},
    "Real Estate": {"Residential REITs": ["AVB", "EQR", "INVH"]},
}
PENNY_UNIVERSE_FALLBACK = {
    "Momentum Watch": {"Under $5, US exchanges": ["BBAI", "SOUN", "KULR", "BITF", "PLUG", "FCEL"]},
}
DIVIDEND_UNIVERSE_FALLBACK = {
    "Energy": {"Coverage List": ["XOM", "CVX", "EPD", "ET"]},
    "Consumer Staples": {"Coverage List": ["KO", "PEP", "PG", "MO"]},
    "Health Care": {"Coverage List": ["PFE", "ABBV", "MRK", "BMY"]},
    "Utilities": {"Coverage List": ["SO", "DUK", "NEE", "D"]},
    "Financials": {"Coverage List": ["JPM", "BAC", "MET", "PRU"]},
    "Communication Services": {"Coverage List": ["VZ", "T", "TMUS"]},
    "Real Estate (REITs)": {"Coverage List": ["O", "SPG", "VICI", "AVB"]},
    "Industrials": {"Coverage List": ["MMM", "EMR", "ITW"]},
}
DIVIDEND_ETFS_FALLBACK = {
    "monthly": ["JEPI", "JEPQ", "SPHD", "QYLD", "XYLD", "RYLD", "SDIV", "DIVO", "PEY", "KBWD"],
    "quarterly": ["SCHD", "VYM", "DVY", "HDV", "VIG", "NOBL", "DGRO", "SPYD", "KNG"],
    "annual": ["FNDF", "FNDC", "FNDE"],
}

UNIVERSE_TTL_SECONDS = 24 * 60 * 60  # re-discover WHICH tickers qualify once a day
DATA_TTL_SECONDS = 20 * 60  # re-pull price/fundamentals for those tickers every 20 min
_cache_lock = threading.Lock()
_caches = {}  # name -> {"timestamp": float, "payload": dict}


def _safe_result(fut, default=None):
    """Never let one bad worker's exception blow up the whole batch — this
    is what let a single ValueError take down the entire /api/dividend
    request even though every OTHER worker had already succeeded."""
    try:
        return fut.result()
    except Exception:
        return default


def discover_sector_universe(industries_per_sector=4, companies_per_industry=5):
    """Live: ask Yahoo for the top industries in each sector (by market
    weight) and the top companies in each industry. Nothing here is
    hardcoded — the industry names AND the tickers both come from Yahoo,
    fresh, every call. Runs fully in parallel — sequentially this is 40+
    blocking network calls, which is what was causing the hang."""

    def get_industries(display_name, key):
        try:
            df = yf.Sector(key).industries
            if df is None or df.empty:
                return display_name, None
            return display_name, df.sort_values("market weight", ascending=False)
        except Exception:
            return display_name, None

    sector_industries = {}
    with ThreadPoolExecutor(max_workers=len(SECTOR_KEYS)) as pool:
        futures = [pool.submit(get_industries, name, key) for name, key in SECTOR_KEYS.items()]
        for fut in as_completed(futures):
            result = _safe_result(fut, (None, None))
            display_name, df = result
            if display_name is not None and df is not None:
                sector_industries[display_name] = df.head(industries_per_sector)

    # flatten every (sector, industry_key) pair we need top_companies for,
    # and fetch them all in one parallel pool rather than nested loops
    industry_tasks = []
    for display_name, df in sector_industries.items():
        for industry_key, row in df.iterrows():
            industry_tasks.append((display_name, industry_key, row.get("name") or industry_key))

    def get_top_companies(display_name, industry_key, industry_name):
        try:
            top_df = yf.Industry(industry_key).top_companies
            if top_df is None or top_df.empty:
                return display_name, industry_name, None
            return display_name, industry_name, list(top_df.index[:companies_per_industry])
        except Exception:
            return display_name, industry_name, None

    universe = {}
    if industry_tasks:
        with ThreadPoolExecutor(max_workers=min(20, len(industry_tasks))) as pool:
            futures = [pool.submit(get_top_companies, *t) for t in industry_tasks]
            for fut in as_completed(futures):
                display_name, industry_name, tickers = _safe_result(fut, (None, None, None))
                if tickers:
                    universe.setdefault(display_name, {})[industry_name] = tickers
    return universe


def _run_screen(query_builder, sort_field, count=25, sort_asc=False):
    """query_builder is a zero-arg callable that CONSTRUCTS and returns the
    query object. Building the query (EquityQuery(...)/ETFQuery(...)) can
    itself raise ValueError on a bad field/value — that exception needs to
    be caught right here too, not just the network call, or one bad query
    takes the whole request down with it."""
    try:
        query = query_builder()
        result = yf.screen(query, count=count, sortField=sort_field, sortAsc=sort_asc)
        quotes = result.get("quotes", []) if isinstance(result, dict) else []
        return [q.get("symbol") for q in quotes if q.get("symbol")]
    except Exception:
        return []


def discover_penny_universe(max_price=5.0, min_avg_volume=300000, per_group=10):
    """Live screen for sub-$5, major-US-exchange names, grouped by TODAY'S
    actual technical pattern rather than a fixed theme list — a fixed list
    of 'AI stocks' or 'biotech stocks' goes stale in weeks; 'what's moving
    right now' is re-asked fresh every cycle."""

    def base_clauses():
        return [
            yf.EquityQuery("eq", ["region", "us"]),
            yf.EquityQuery("is-in", ["exchange"] + MAIN_US_EXCHANGES),
            yf.EquityQuery("lt", ["intradayprice", max_price]),
            yf.EquityQuery("gt", ["avgdailyvol3m", min_avg_volume]),
        ]

    def build_gainers_q():
        return yf.EquityQuery("and", base_clauses() + [yf.EquityQuery("gt", ["percentchange", 3])])

    def build_volume_q():
        return yf.EquityQuery("and", base_clauses() + [yf.EquityQuery("gt", ["dayvolume", min_avg_volume * 2])])

    groups = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_gainers = pool.submit(_run_screen, build_gainers_q, "percentchange", per_group)
        f_active = pool.submit(_run_screen, build_volume_q, "dayvolume", per_group)
        gainers = _safe_result(f_gainers, [])
        active = _safe_result(f_active, [])
    if gainers:
        groups["Today's Momentum Gainers"] = {"Under $5 · major US exchanges": gainers}
    if active:
        groups["Today's Unusual Volume"] = {"Under $5 · major US exchanges": active}

    ladder_groups = discover_monthly_gainers_ladder()
    groups.update(ladder_groups)
    return groups


# Gain tiers for the monthly-gainers ladder: (label, floor%, ceiling% or None
# for open-ended). Exclusive ranges so every stock lands in exactly ONE tier
# — the one that actually describes it — instead of duplicating a 250%
# mover across six overlapping "above X%" lists.
GAIN_TIERS = [
    ("200%+", 200, None),
    ("100%–200%", 100, 200),
    ("70%–100%", 70, 100),
    ("50%–70%", 50, 70),
    ("30%–50%", 30, 50),
    ("10%–30%", 10, 30),
]


def discover_monthly_gainers_ladder(min_avg_volume=200000, max_per_tier=12):
    """Live: Yahoo's screener has no native '% change over 1 month' field,
    so this casts a broad net for names that have moved recently (today's
    biggest gainers + biggest 52-week movers, major US exchanges, a
    liquidity floor, no price ceiling), then COMPUTES every candidate's real
    trailing 1-month price return from actual price history and sorts each
    one into the gain tier it genuinely belongs in — measured, not assumed,
    and nothing here is a fixed list of "hot stocks."""

    def build_today_gainers_q():
        return yf.EquityQuery("and", [
            yf.EquityQuery("eq", ["region", "us"]),
            yf.EquityQuery("is-in", ["exchange"] + MAIN_US_EXCHANGES),
            yf.EquityQuery("gt", ["avgdailyvol3m", min_avg_volume]),
            yf.EquityQuery("gt", ["percentchange", 5]),
        ])

    def build_52wk_movers_q():
        return yf.EquityQuery("and", [
            yf.EquityQuery("eq", ["region", "us"]),
            yf.EquityQuery("is-in", ["exchange"] + MAIN_US_EXCHANGES),
            yf.EquityQuery("gt", ["avgdailyvol3m", min_avg_volume]),
            yf.EquityQuery("gt", ["fiftytwowkpercentchange", 30]),
        ])

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_run_screen, build_today_gainers_q, "percentchange", 60)
        f2 = pool.submit(_run_screen, build_52wk_movers_q, "fiftytwowkpercentchange", 60)
        candidates = list(dict.fromkeys(_safe_result(f1, []) + _safe_result(f2, [])))

    if not candidates:
        return {}

    returns = fetch_all(candidates, fetch_fn=fetch_etf_returns)
    tiered = {label: [] for label, _, _ in GAIN_TIERS}
    for sym, data in returns.items():
        if not data or data.get("return_1m") is None:
            continue
        r = data["return_1m"]
        for label, floor, ceiling in GAIN_TIERS:
            if r >= floor and (ceiling is None or r < ceiling):
                tiered[label].append({"symbol": sym, "return_1m": r})
                break

    groups = {}
    for label, _, _ in GAIN_TIERS:
        rows = sorted(tiered[label], key=lambda d: d["return_1m"], reverse=True)[:max_per_tier]
        if rows:
            groups[f"Monthly Gainers: {label}"] = {
                f"Real 1-month return in [{label}) · major US exchanges": [r["symbol"] for r in rows]
            }
    return groups


def discover_dividend_universe(min_yield_pct=2.5, per_sector=6):
    """Live screen, per sector, for US names actually yielding above
    min_yield_pct right now — replaces a fixed 'these are the dividend
    stocks' list with 'these are CURRENTLY the highest-yielding names'.
    The 8 per-sector screens run in parallel."""

    def screen_sector(display_name, yahoo_sector):
        def build_q():
            return yf.EquityQuery("and", [
                yf.EquityQuery("eq", ["region", "us"]),
                yf.EquityQuery("is-in", ["exchange"] + MAIN_US_EXCHANGES),
                yf.EquityQuery("eq", ["sector", yahoo_sector]),
                yf.EquityQuery("gt", ["forward_dividend_yield", min_yield_pct]),
            ])
        return display_name, _run_screen(build_q, "forward_dividend_yield", count=per_sector)

    universe = {}
    with ThreadPoolExecutor(max_workers=len(SCREENER_SECTOR_NAMES)) as pool:
        futures = [pool.submit(screen_sector, name, sec) for name, sec in SCREENER_SECTOR_NAMES.items()]
        for fut in as_completed(futures):
            display_name, tickers = _safe_result(fut, (None, []))
            if display_name is not None and tickers:
                universe[display_name] = {"Coverage List": tickers}
    return universe


def _classify_dividend_frequency(symbol):
    """Look at ~14 months of ACTUAL distributions to determine how often a
    fund really pays, instead of assuming based on its category."""
    try:
        divs = yf.Ticker(symbol).dividends
        if divs is None or divs.empty:
            return None
        cutoff = divs.index.max() - pd.Timedelta(days=420)
        n = int((divs.index >= cutoff).sum())
        if n >= 9:
            return "monthly"
        if 3 <= n <= 6:
            return "quarterly"
        if 1 <= n <= 2:
            return "annual"
        return None
    except Exception:
        return None


# Valid Morningstar category names, per yfinance's own ETF_SCREENER_EQ_MAP —
# "Equity Income" / "Derivative Income" are NOT valid values (that was the
# bug); the real category for covered-call/derivative-income funds is
# "Option Writing".
ETF_INCOME_CATEGORIES = ["Option Writing", "Large Value", "Preferred Stock", "Real Estate"]


def discover_dividend_etfs(per_bucket=12):
    """Live screen for candidate US income ETFs, then classify each one's
    REAL payout frequency from its actual distribution history rather than
    a static monthly/quarterly/annual assignment. The per-candidate history
    lookups (up to 60 of them) run in parallel — this was the single
    biggest source of the "keeps loading" hang when done sequentially."""

    def build_q():
        return yf.ETFQuery("and", [
            yf.ETFQuery("eq", ["region", "us"]),
            yf.ETFQuery("is-in", ["categoryname"] + ETF_INCOME_CATEGORIES),
        ])

    candidates = _run_screen(build_q, "annualreturnnavy3", count=60)

    buckets = {"monthly": [], "quarterly": [], "annual": []}
    if not candidates:
        return buckets
    with ThreadPoolExecutor(max_workers=min(20, len(candidates))) as pool:
        futures = {pool.submit(_classify_dividend_frequency, sym): sym for sym in candidates}
        for fut in as_completed(futures):
            freq = _safe_result(fut, None)
            if freq and len(buckets[freq]) < per_bucket:
                buckets[freq].append(futures[fut])
    return buckets


def get_universe(name, discover_fn, fallback):
    """Daily-cached universe discovery, with a static fallback ONLY if the
    live call comes back empty (network issue, rate limit, etc.)."""
    universe = get_cached(f"universe_{name}", discover_fn, ttl=UNIVERSE_TTL_SECONDS)
    return universe if universe else fallback


# ---------------------------------------------------------------------------
# 1b. MACRO SNAPSHOT — a small set of live macro indicators, used both to
#     give the rep something current to talk about, and as a light, fully
#     explainable tilt on the penny/dividend scoring below (never a black
#     box: every tilt is named in the "why" text).
# ---------------------------------------------------------------------------
MACRO_TICKERS = {
    "10-Year Treasury Yield": "^TNX",
    "Volatility (VIX)": "^VIX",
    "US Dollar Index": "DX-Y.NYB",
    "S&P 500": "^GSPC",
}


def build_macro_payload():
    indicators = {}
    for label, sym in MACRO_TICKERS.items():
        d = fetch_ticker(sym)
        if not d:
            continue
        value = d["price"] / 10 if sym == "^TNX" else d["price"]  # Yahoo quotes ^TNX as yield x10
        indicators[label] = {
            "symbol": sym, "value": round(value, 2), "pct_5d": d["pct_5d"], "sparkline": d["sparkline"],
        }

    vix = indicators.get("Volatility (VIX)", {}).get("value")
    yield_chg = indicators.get("10-Year Treasury Yield", {}).get("pct_5d")
    regime = {
        "risk_off": bool(vix is not None and vix >= 20),
        "rates_rising": bool(yield_chg is not None and yield_chg > 1.5),
    }
    notes = []
    if regime["risk_off"]:
        notes.append("VIX is elevated — momentum picks are scored more conservatively today (extra weight on confirmed volume over raw price moves).")
    if regime["rates_rising"]:
        notes.append("The 10-year yield has risen over the past week — income picks are scored with extra weight on stability over raw yield.")
    if not notes:
        notes.append("No major macro tilt today — standard weighting applied.")

    return {
        "generated_at": time.time(),
        "indicators": indicators,
        "regime": regime,
        "note": " ".join(notes),
    }


# ---------------------------------------------------------------------------
# 2. DATA FETCH
# ---------------------------------------------------------------------------
def fetch_ticker(symbol):
    """Pull 5-day price action + fundamentals for one ticker."""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="8d", interval="1d")
        hist = hist[hist["Close"].notna()]
        if hist.empty or len(hist) < 2:
            return None
        closes = hist["Close"].tail(5)
        pct_5d = (closes.iloc[-1] / closes.iloc[0] - 1) * 100

        info = {}
        try:
            info = t.get_info()
        except Exception:
            try:
                info = t.info
            except Exception:
                info = {}

        return {
            "symbol": symbol,
            "name": info.get("shortName") or info.get("longName") or symbol,
            "price": round(float(closes.iloc[-1]), 2),
            "sparkline": [round(float(x), 2) for x in closes.tolist()],
            "pct_5d": round(float(pct_5d), 2),
            "trailingPE": info.get("trailingPE"),
            "forwardPE": info.get("forwardPE"),
            "revenueGrowth": info.get("revenueGrowth"),
            "profitMargins": info.get("profitMargins"),
            "returnOnEquity": info.get("returnOnEquity"),
            "debtToEquity": info.get("debtToEquity"),
            "recommendationMean": info.get("recommendationMean"),
            "recommendationKey": info.get("recommendationKey"),
            "targetMeanPrice": info.get("targetMeanPrice"),
            "dividendYield": info.get("dividendYield"),
            "marketCap": info.get("marketCap"),
            "totalAssets": info.get("totalAssets") or info.get("netAssets"),
            "companySector": info.get("sector"),
            "companyIndustry": info.get("industry"),
            # -- extra micro-fundamental depth --
            "earningsGrowth": info.get("earningsQuarterlyGrowth"),
            "currentRatio": info.get("currentRatio"),
            # -- momentum / liquidity (penny stock radar) --
            "averageVolume": info.get("averageVolume"),
            "volume": info.get("volume") or info.get("regularMarketVolume"),
            "fiftyTwoWeekHigh": info.get("fiftyTwoWeekHigh"),
            "fiftyTwoWeekLow": info.get("fiftyTwoWeekLow"),
            # -- income quality (dividend & income desk) --
            "payoutRatio": info.get("payoutRatio"),
            "beta": info.get("beta"),
            # -- ETF-flavored fields (harmless None for regular stocks) --
            "category": info.get("category"),
            "fundYield": info.get("yield"),
            "expenseRatio": info.get("annualReportExpenseRatio") or info.get("netExpenseRatio"),
            "threeYearAverageReturn": info.get("threeYearAverageReturn"),
        }
    except Exception:
        return None


def fetch_all(symbols, fetch_fn=fetch_ticker):
    results = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(fetch_fn, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            data = fut.result()
            if data:
                results[sym] = data
    return results


# ---------------------------------------------------------------------------
# 3. SCORING — normalized WITHIN each sector, since "cheap" for a bank and
#    "cheap" for a software company are different numbers.
# ---------------------------------------------------------------------------
def _normalize(values, invert=False):
    """values: dict symbol -> float. Returns dict symbol -> 0-100 score."""
    if not values:
        return {}
    arr = np.array(list(values.values()), dtype=float)
    lo, hi = arr.min(), arr.max()
    out = {}
    for k, v in values.items():
        score = 50.0 if hi == lo else (v - lo) / (hi - lo) * 100
        out[k] = 100 - score if invert else score
    return out


def score_sector(tickers_data):
    """tickers_data: list of ticker dicts (already fetched). Returns dict
    symbol -> {score, breakdown}"""

    def collect(key, cond=lambda v: v is not None):
        return {d["symbol"]: d[key] for d in tickers_data if cond(d.get(key))}

    pe_vals = {}
    for d in tickers_data:
        pe = d.get("forwardPE") or d.get("trailingPE")
        if pe and pe > 0:
            pe_vals[d["symbol"]] = pe

    upside_vals = {}
    for d in tickers_data:
        tgt, price = d.get("targetMeanPrice"), d.get("price")
        if tgt and price:
            upside_vals[d["symbol"]] = (tgt / price - 1) * 100

    components = {
        "valuation": (_normalize(pe_vals, invert=True), 0.22),
        "growth_revenue": (_normalize(collect("revenueGrowth")), 0.13),
        "growth_earnings": (_normalize(collect("earningsGrowth")), 0.10),
        "profitability": (_normalize(collect("profitMargins")), 0.08),
        "returns": (_normalize(collect("returnOnEquity")), 0.08),
        "solvency": (_normalize(collect("currentRatio")), 0.07),
        "analyst": (_normalize(collect("recommendationMean"), invert=True), 0.18),
        "upside": (_normalize(upside_vals), 0.09),
        "momentum": (_normalize(collect("pct_5d")), 0.05),
    }

    scores = {}
    for d in tickers_data:
        sym = d["symbol"]
        total, wsum, breakdown = 0.0, 0.0, {}
        for name, (score_map, weight) in components.items():
            if sym in score_map:
                total += score_map[sym] * weight
                wsum += weight
                breakdown[name] = round(score_map[sym], 1)
        final = round(total / wsum, 1) if wsum > 0 else 0.0
        scores[sym] = {"score": final, "breakdown": breakdown}

    avg_pe = round(float(np.mean(list(pe_vals.values()))), 1) if pe_vals else None
    return scores, avg_pe


def rating_from_score(score):
    if score >= 75:
        return "Strong Buy"
    if score >= 60:
        return "Buy"
    if score >= 40:
        return "Hold"
    return "Sell"


def build_reason(d, sector_avg_pe, rating):
    parts = []
    pe = d.get("forwardPE") or d.get("trailingPE")
    pe_label = "fwd P/E" if d.get("forwardPE") else "P/E"
    if pe:
        if sector_avg_pe:
            cmp_word = "below" if pe < sector_avg_pe else "above"
            parts.append(f"{pe_label} {pe:.1f}x ({cmp_word} sector avg {sector_avg_pe:.1f}x)")
        else:
            parts.append(f"{pe_label} {pe:.1f}x")
    if d.get("revenueGrowth") is not None:
        parts.append(f"revenue growth {d['revenueGrowth']*100:.1f}%")
    if d.get("earningsGrowth") is not None:
        parts.append(f"earnings growth {d['earningsGrowth']*100:.1f}%")
    if d.get("returnOnEquity") is not None:
        parts.append(f"ROE {d['returnOnEquity']*100:.1f}%")
    if d.get("recommendationMean") is not None:
        parts.append(f"analyst consensus {d['recommendationMean']:.1f}/5")
    if d.get("targetMeanPrice") and d.get("price"):
        upside = (d["targetMeanPrice"] / d["price"] - 1) * 100
        direction = "upside" if upside >= 0 else "downside"
        parts.append(f"{abs(upside):.1f}% {direction} to ${d['targetMeanPrice']:.0f} target")
    parts.append(f"{d['pct_5d']:+.1f}% over 5 days")

    fact_string = "; ".join(parts)
    lead = {
        "Strong Buy": "Strongest setup in the sector right now",
        "Buy": "Best fundamentals-backed pick in the sector",
        "Hold": "Best available in the sector, but mixed picture",
        "Sell": "Weakest sector — even the top name has issues",
    }[rating]
    return f"{lead}: {fact_string}."


# ---------------------------------------------------------------------------
# 3b. PENNY STOCK SCORING — short-term momentum + unusual volume, NOT a
#     fundamentals call. Ratings are deliberately labeled differently
#     (Hot/Building/Neutral/Cooling) so they never read as an investment
#     recommendation.
# ---------------------------------------------------------------------------
def score_penny(tickers_data, regime=None):
    regime = regime or {}
    mom_vals = {d["symbol"]: d["pct_5d"] for d in tickers_data if d.get("pct_5d") is not None}
    mom_score = _normalize(mom_vals)

    vol_ratio_vals = {}
    for d in tickers_data:
        avgv, curv = d.get("averageVolume"), d.get("volume")
        if avgv and curv and avgv > 0:
            vol_ratio_vals[d["symbol"]] = curv / avgv
    vol_score = _normalize(vol_ratio_vals)

    range_score = {}
    for d in tickers_data:
        hi, lo, price = d.get("fiftyTwoWeekHigh"), d.get("fiftyTwoWeekLow"), d.get("price")
        if hi and lo and price and hi > lo:
            pos = (price - lo) / (hi - lo)
            # peak reward around the lower-middle of the range (room to run,
            # already showing some strength) rather than at either extreme
            range_score[d["symbol"]] = max(0.0, 100 - abs(pos - 0.35) * 130)

    # real trailing 1-month return, where available (e.g. the 300%+ monthly
    # breakout group) — only present on a subset of tickers, so this weight
    # simply drops out of the average for tickers that don't have it
    breakout_vals = {d["symbol"]: d["return_1m"] for d in tickers_data if d.get("return_1m") is not None}
    breakout_score = _normalize(breakout_vals)

    # macro tilt: in a risk-off tape (VIX elevated), raw momentum is less
    # trustworthy — lean more on confirmed volume instead
    mom_w, vol_w, range_w = (0.40, 0.45, 0.15) if regime.get("risk_off") else (0.55, 0.30, 0.15)

    weights = {
        "momentum": (mom_score, mom_w),
        "volume": (vol_score, vol_w),
        "range": (range_score, range_w),
        "breakout": (breakout_score, 0.25),
    }
    scores = {}
    for d in tickers_data:
        sym = d["symbol"]
        total, wsum = 0.0, 0.0
        for _, (score_map, w) in weights.items():
            if sym in score_map:
                total += score_map[sym] * w
                wsum += w
        scores[sym] = round(total / wsum, 1) if wsum > 0 else 0.0
    return scores


def rating_penny(score):
    if score >= 65:
        return "Hot"
    if score >= 45:
        return "Building"
    if score >= 25:
        return "Neutral"
    return "Cooling"


def build_penny_reason(d, score, rating):
    parts = [f"{d['pct_5d']:+.1f}% over 5 days"]
    if d.get("return_1m") is not None:
        parts.insert(0, f"{d['return_1m']:+.1f}% over the past month")
    avgv, curv = d.get("averageVolume"), d.get("volume")
    if avgv and curv:
        ratio = curv / avgv
        tag = " (unusual activity)" if ratio >= 1.5 else ""
        parts.append(f"trading at {ratio:.1f}x average volume{tag}")
    hi, lo, price = d.get("fiftyTwoWeekHigh"), d.get("fiftyTwoWeekLow"), d.get("price")
    if hi and lo and price and hi > lo:
        pos = (price - lo) / (hi - lo) * 100
        parts.append(f"sitting {pos:.0f}% of the way through its 52-week range")
    fact = "; ".join(parts)
    lead = {
        "Hot": "Highest short-term momentum in this group",
        "Building": "Momentum building, not yet extended",
        "Neutral": "Range-bound — no clear signal yet",
        "Cooling": "Momentum fading versus the rest of the group",
    }[rating]
    return (
        f"{lead}: {fact}. Speculative — high volatility and thin liquidity. "
        f"Confirm float, catalyst and spread, and size/stop it accordingly before "
        f"discussing on a call."
    )


# ---------------------------------------------------------------------------
# 3c. DIVIDEND & INCOME SCORING — yield quality (not just raw yield),
#     payout sustainability, capital-value momentum, and price stability.
# ---------------------------------------------------------------------------
def score_dividend(tickers_data, regime=None):
    regime = regime or {}
    # yield "quality" peaks around 5.5% — very high yields often signal
    # distress or an unsustainable payout rather than genuine value
    yield_vals = {d["symbol"]: d["dividendYield"] * 100 for d in tickers_data if d.get("dividendYield")}
    yield_score = {k: max(0.0, 100 - abs(v - 5.5) / 6 * 100) for k, v in yield_vals.items()}

    payout_score = {}
    for d in tickers_data:
        pr = d.get("payoutRatio")
        if pr is None:
            continue
        v = pr * 100
        sym = d["symbol"]
        if v <= 0:
            payout_score[sym] = 40.0
        elif v <= 75:
            payout_score[sym] = 100 - (v / 75) * 30
        elif v <= 100:
            payout_score[sym] = 70 - (v - 75) / 25 * 40
        else:
            payout_score[sym] = max(0.0, 30 - (v - 100) / 50 * 30)

    mom_vals = {d["symbol"]: d["pct_5d"] for d in tickers_data if d.get("pct_5d") is not None}
    mom_score = _normalize(mom_vals)

    beta_vals = {d["symbol"]: d["beta"] for d in tickers_data if d.get("beta") is not None}
    stability_score = _normalize(beta_vals, invert=True)

    # macro tilt: with the 10-year yield rising, lean further into safety
    # (low payout, low beta) and pull weight off raw headline yield
    if regime.get("rates_rising"):
        yield_w, payout_w, mom_w, stability_w = 0.25, 0.30, 0.20, 0.25
    else:
        yield_w, payout_w, mom_w, stability_w = 0.35, 0.30, 0.20, 0.15

    weights = {
        "yield": (yield_score, yield_w),
        "payout": (payout_score, payout_w),
        "momentum": (mom_score, mom_w),
        "stability": (stability_score, stability_w),
    }
    scores = {}
    for d in tickers_data:
        sym = d["symbol"]
        total, wsum = 0.0, 0.0
        for _, (score_map, w) in weights.items():
            if sym in score_map:
                total += score_map[sym] * w
                wsum += w
        scores[sym] = round(total / wsum, 1) if wsum > 0 else 0.0
    return scores


def rating_dividend(score):
    if score >= 70:
        return "Strong Income Pick"
    if score >= 55:
        return "Income Pick"
    if score >= 35:
        return "Watch"
    return "Caution"


def build_dividend_reason(d, score, rating):
    parts = []
    if d.get("dividendYield"):
        parts.append(f"yielding {d['dividendYield']*100:.1f}%")
    if d.get("payoutRatio") is not None:
        parts.append(f"{d['payoutRatio']*100:.0f}% payout ratio")
    if d.get("beta") is not None:
        parts.append(f"beta {d['beta']:.2f}")
    parts.append(f"{d['pct_5d']:+.1f}% over 5 days")
    fact = "; ".join(parts)
    lead = {
        "Strong Income Pick": "Best income + stability combination in the sector",
        "Income Pick": "Solid income candidate",
        "Watch": "Decent yield, but keep an eye on sustainability",
        "Caution": "Headline yield looks attractive, but payout or stability is a flag",
    }[rating]
    return f"{lead}: {fact}."


# ---------------------------------------------------------------------------
# 3e. DIVIDEND ETF SCORING — same 0-100 composite → Strong Buy/Buy/Hold/Sell
#     scale as the main Sector Leaders tool (rating_from_score), so all three
#     ETF buckets speak the same "recommendation" language you asked for.
#     Scored WITHIN its own frequency bucket (monthly covered-call funds run
#     structurally higher yields than quarterly quality funds, so comparing
#     across buckets would be apples-to-oranges).
# ---------------------------------------------------------------------------
def score_etf(etf_list):
    yield_vals = {}
    for e in etf_list:
        dy = e.get("dividendYield") or e.get("fundYield")
        if dy:
            yield_vals[e["symbol"]] = dy * 100
    yield_score = _normalize(yield_vals)

    expense_vals = {e["symbol"]: e["expenseRatio"] for e in etf_list if e.get("expenseRatio") is not None}
    expense_score = _normalize(expense_vals, invert=True)

    ret_vals = {
        e["symbol"]: e["threeYearAverageReturn"] for e in etf_list if e.get("threeYearAverageReturn") is not None
    }
    ret_score = _normalize(ret_vals)

    mom_vals = {e["symbol"]: e["pct_5d"] for e in etf_list if e.get("pct_5d") is not None}
    mom_score = _normalize(mom_vals)

    weights = {
        "yield": (yield_score, 0.40),
        "expense": (expense_score, 0.20),
        "total_return": (ret_score, 0.30),
        "momentum": (mom_score, 0.10),
    }
    scores = {}
    for e in etf_list:
        sym = e["symbol"]
        total, wsum = 0.0, 0.0
        for _, (score_map, w) in weights.items():
            if sym in score_map:
                total += score_map[sym] * w
                wsum += w
        scores[sym] = round(total / wsum, 1) if wsum > 0 else 0.0
    return scores


def build_etf_reason(d, score, rating):
    parts = []
    dy = d.get("dividendYield") or d.get("fundYield")
    if dy:
        parts.append(f"yielding {dy*100:.1f}%")
    if d.get("expenseRatio") is not None:
        parts.append(f"{d['expenseRatio']*100:.2f}% expense ratio")
    if d.get("threeYearAverageReturn") is not None:
        parts.append(f"{d['threeYearAverageReturn']*100:.1f}% 3-year avg return")
    parts.append(f"{d['pct_5d']:+.1f}% over 5 days")
    fact = "; ".join(parts)
    lead = {
        "Strong Buy": "Best yield-for-cost-and-return combination in this bucket",
        "Buy": "Solid income vehicle in this bucket",
        "Hold": "Fair option, but a peer in this bucket looks stronger",
        "Sell": "Weakest yield/cost/return combination in this bucket right now",
    }[rating]
    return f"{lead}: {fact}."


def build_etf_bucket(tickers):
    fetched = fetch_all(tickers)
    data = list(fetched.values())
    scores = score_etf(data) if data else {}
    rows = []
    for d in data:
        sc = scores.get(d["symbol"], 0.0)
        rating = rating_from_score(sc)
        rows.append({**d, "score": sc, "rating": rating, "reason": build_etf_reason(d, sc, rating)})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# 3e. HIGH-YIELD / OPTION-INCOME ETF FAMILIES — a curated, explicitly-named
#     directory (single-stock covered-call/synthetic-income funds, 0DTE
#     index funds, and the more established option-income shops). Unlike
#     the rest of the app, WHICH TICKERS belong to WHICH family is fixed on
#     purpose — these are specific named products from specific issuers, not
#     something a generic sector/category screen reliably reconstructs. The
#     PRICE, YIELD, RETURNS, and RATING for every one of them are still 100%
#     live — nothing about the numbers is static.
#
#     This is a fast-moving corner of the ETF market: funds launch, close,
#     and get renamed often. Treat tickers here the same way as the penny
#     stock list — verify before relying on them, and expect to update this
#     periodically.
# ---------------------------------------------------------------------------
ETF_FAMILIES = {
    "YieldMax — Mega-Cap Tech": ["APLY", "AMZY", "GOOY", "FBY", "MSFO", "NFLY", "DISO"],
    "YieldMax — AI & Semiconductors": ["AIYY", "AMDY", "INYY", "SMCY", "NVDY", "SNOY", "MRNY"],
    "YieldMax — Crypto": ["MSTY", "WNTR", "CONY", "FIAT", "MARO"],
    "YieldMax — Consumer / Internet": ["PYPY", "SQY", "ABNY", "RDYY", "RBLY", "CVNY", "HIYY"],
    "YieldMax — Auto": ["TSLY", "CRSH"],
    "YieldMax — Other": ["ULTY", "CRCO"],
    "GraniteShares YieldBOOST — Technology": ["AMYY", "HMYY", "MUYY", "IOYY", "SMYY", "RGYY", "QBY", "CWY", "SEMY"],
    "GraniteShares YieldBOOST — Crypto": ["MAAY", "RTYY", "CRY"],
    "Roundhill — WeeklyPay": ["ARMW", "AMDW", "HOOW"],
    "Roundhill — Covered Call": ["YETH"],
    "ARK 21Shares": ["ARKA", "ARKC", "ARKY"],
    "Defiance ETFs": ["SOUX", "QQQY", "JEPY", "IWMY", "SPYT"],
    "JPMorgan Income ETFs": ["JEPI", "JEPQ", "JPIE"],
    "Global X Covered Call Family": ["QYLD", "XYLD", "RYLD", "QCLR", "XRMI"],
    "NEOS Income ETFs": ["SPYI", "QQQI", "IWMI"],
    "RexShares / MicroStrategy Theme": ["MSTY", "MSTX", "MSTU"],
    "Crypto & Option-Income ETFs": ["YETH", "CONY", "MSTY", "FIAT", "MARO", "BEGS"],
    "Monthly Income — Conservative": ["JEPI", "JEPQ", "SPYI", "QQQI"],
    "Monthly Income — Aggressive": ["QYLD", "XYLD", "RYLD", "IWMY", "JEPY", "QQQY"],
    "Monthly Income — Ultra-High Yield": ["MSTY", "CONY", "NVDY", "TSLY", "ULTY", "SMCY", "MARO", "FIAT", "WNTR", "CRCO"],
    "Monthly Income — Crypto-Oriented": ["MSTY", "CONY", "MARO", "MAAY", "RTYY", "ARKA", "ARKC", "ARKY"],
}

# Static risk classification by product structure (not live data): single-
# stock synthetic/covered-call and leveraged crypto-linked funds carry
# materially more concentration, volatility, and NAV-erosion risk than
# diversified index option-income funds.
FAMILY_RISK_TIER = {
    "YieldMax — Mega-Cap Tech": "Very High",
    "YieldMax — AI & Semiconductors": "Very High",
    "YieldMax — Crypto": "Very High",
    "YieldMax — Consumer / Internet": "Very High",
    "YieldMax — Auto": "Very High",
    "YieldMax — Other": "Very High",
    "GraniteShares YieldBOOST — Technology": "Very High",
    "GraniteShares YieldBOOST — Crypto": "Very High",
    "Roundhill — WeeklyPay": "Very High",
    "Roundhill — Covered Call": "Very High",
    "ARK 21Shares": "Very High",
    "Defiance ETFs": "High",
    "JPMorgan Income ETFs": "Moderate",
    "Global X Covered Call Family": "Medium",
    "NEOS Income ETFs": "Medium",
    "RexShares / MicroStrategy Theme": "Very High",
    "Crypto & Option-Income ETFs": "Very High",
    "Monthly Income — Conservative": "Medium",
    "Monthly Income — Aggressive": "High",
    "Monthly Income — Ultra-High Yield": "Very High",
    "Monthly Income — Crypto-Oriented": "Very High",
}


def fetch_etf_returns(symbol):
    """1-year price history → real 1-month/3-month/YTD total price return.
    Despite the name, this works for any ticker (stock or fund) — used both
    to catch NAV erosion on income ETFs and to measure real 1-month gains
    for the monthly-breakout stock screen."""
    try:
        hist = yf.Ticker(symbol).history(period="1y")
        hist = hist[hist["Close"].notna()]
        if hist.empty:
            return None
        closes = hist["Close"]
        last = float(closes.iloc[-1])

        def trailing_return(n_days):
            if len(closes) <= n_days:
                return None
            return round((last / float(closes.iloc[-n_days - 1]) - 1) * 100, 2)

        this_year = closes.index.max().year
        ytd_slice = closes[closes.index.year == this_year]
        ytd_return = round((last / float(ytd_slice.iloc[0]) - 1) * 100, 2) if len(ytd_slice) else None

        return {"return_1m": trailing_return(21), "return_3m": trailing_return(63), "return_ytd": ytd_return}
    except Exception:
        return None


def score_etf_family(etf_list):
    """Weighted toward TOTAL RETURN, not just yield — a fund can carry a
    huge headline distribution while its price (NAV) quietly erodes faster
    than the payout. Rewarding total-return quality over raw yield directly
    answers 'is this actually a good value, or just a big number.'"""
    yield_vals = {}
    for e in etf_list:
        dy = e.get("dividendYield") or e.get("fundYield")
        if dy:
            yield_vals[e["symbol"]] = dy * 100
    yield_score = _normalize(yield_vals)

    expense_vals = {e["symbol"]: e["expenseRatio"] for e in etf_list if e.get("expenseRatio") is not None}
    expense_score = _normalize(expense_vals, invert=True)

    ret_vals = {}
    for e in etf_list:
        r = e.get("return_ytd")
        if r is None:
            r = e.get("return_3m")
        if r is not None:
            ret_vals[e["symbol"]] = r
    ret_score = _normalize(ret_vals)

    mom_vals = {e["symbol"]: e["pct_5d"] for e in etf_list if e.get("pct_5d") is not None}
    mom_score = _normalize(mom_vals)

    weights = {
        "yield": (yield_score, 0.35),
        "expense": (expense_score, 0.15),
        "total_return": (ret_score, 0.40),
        "momentum": (mom_score, 0.10),
    }
    scores = {}
    for e in etf_list:
        sym = e["symbol"]
        total, wsum = 0.0, 0.0
        for _, (score_map, w) in weights.items():
            if sym in score_map:
                total += score_map[sym] * w
                wsum += w
        scores[sym] = round(total / wsum, 1) if wsum > 0 else 0.0
    return scores


def build_etf_family_reason(d, score, rating, risk_tier):
    parts = []
    dy = d.get("dividendYield") or d.get("fundYield")
    if dy:
        parts.append(f"distribution yield {dy*100:.1f}%")
    if d.get("return_ytd") is not None:
        parts.append(f"{d['return_ytd']:+.1f}% YTD price return")
    if d.get("return_3m") is not None:
        parts.append(f"{d['return_3m']:+.1f}% over 3 months")
    if d.get("expenseRatio") is not None:
        parts.append(f"{d['expenseRatio']*100:.2f}% expense ratio")
    fact = "; ".join(parts)
    lead = {
        "Strong Buy": "Best yield-vs-total-return combination in this family right now",
        "Buy": "Solid option within this family",
        "Hold": "Middle of the pack in this family",
        "Sell": "Weakest yield/total-return combination in this family right now",
    }[rating]

    nav_flag = ""
    if d.get("return_ytd") is not None and dy and d["return_ytd"] < -10 and dy > 0.15:
        nav_flag = (" Caution: a big headline yield alongside a falling price usually means the "
                    "distribution isn't fully offsetting NAV erosion — look at total return, not yield alone.")

    return f"{lead}: {fact}. Risk tier: {risk_tier}.{nav_flag}"


def build_etf_families_payload():
    all_tickers = sorted({t for lst in ETF_FAMILIES.values() for t in lst})
    base = fetch_all(all_tickers)
    returns = fetch_all(all_tickers, fetch_fn=fetch_etf_returns)
    merged = {sym: {**d, **(returns.get(sym) or {})} for sym, d in base.items()}

    families_out = {}
    for family, tickers in ETF_FAMILIES.items():
        data = [merged[t] for t in tickers if t in merged]
        risk_tier = FAMILY_RISK_TIER.get(family, "High")
        scores = score_etf_family(data) if data else {}
        rows = []
        for d in data:
            sc = scores.get(d["symbol"], 0.0)
            rating = rating_from_score(sc)
            rows.append({
                **d, "score": sc, "rating": rating, "risk_tier": risk_tier,
                "reason": build_etf_family_reason(d, sc, rating, risk_tier),
            })
        rows.sort(key=lambda r: r["score"], reverse=True)
        for i, r in enumerate(rows, start=1):
            r["family_rank"] = i
        families_out[family] = {"risk_tier": risk_tier, "rows": rows}

    return {"generated_at": time.time(), "families": families_out}


# ---------------------------------------------------------------------------
# 3d. GENERIC PAYLOAD BUILDER — shared shape for penny + dividend modes so
#     the frontend can render both with the same components as the main
#     sector tool.
# ---------------------------------------------------------------------------
def build_universe_payload(universe, score_fn, rating_fn, reason_fn, fetch_fn=fetch_ticker, extra_fetch_fn=None):
    all_symbols = sorted({s for sub in universe.values() for lst in sub.values() for s in lst})
    fetched = fetch_all(all_symbols, fetch_fn=fetch_fn)

    if extra_fetch_fn:
        extra = fetch_all(all_symbols, fetch_fn=extra_fetch_fn)
        for sym, d in fetched.items():
            if sym in extra and extra[sym]:
                d.update(extra[sym])

    groups_out = {}
    call_sheet = []

    for group, subgroups in universe.items():
        group_symbols = [s for lst in subgroups.values() for s in lst]
        group_data = [fetched[s] for s in group_symbols if s in fetched]
        scores = score_fn(group_data) if group_data else {}

        sub_out = {}
        for sub, symbols in subgroups.items():
            rows = []
            for s in symbols:
                if s not in fetched:
                    continue
                d = fetched[s]
                sc = scores.get(s, 0.0)
                rows.append({**d, "score": sc, "rating": rating_fn(sc)})
            rows.sort(key=lambda r: r["score"], reverse=True)
            sub_out[sub] = rows

        if group_data and scores:
            best_symbol = max(scores, key=lambda s: scores[s])
            best = fetched[best_symbol]
            best_score = scores[best_symbol]
            best_rating = rating_fn(best_score)
            reason = reason_fn(best, best_score, best_rating)
            pick = {**best, "score": best_score, "rating": best_rating, "reason": reason}
        else:
            pick = None

        groups_out[group] = {"subindustries": sub_out, "pick": pick}
        if pick:
            call_sheet.append({"sector": group, **pick})

    return {"generated_at": time.time(), "sectors": groups_out, "call_sheet": call_sheet}


def build_penny_payload():
    macro = get_cached("macro", build_macro_payload, ttl=DATA_TTL_SECONDS)
    regime = macro.get("regime", {})
    universe = get_universe("penny", discover_penny_universe, PENNY_UNIVERSE_FALLBACK)
    payload = build_universe_payload(
        universe, lambda data: score_penny(data, regime), rating_penny, build_penny_reason,
        extra_fetch_fn=fetch_etf_returns,
    )
    payload["macro"] = macro
    return payload


def build_dividend_payload():
    macro = get_cached("macro", build_macro_payload, ttl=DATA_TTL_SECONDS)
    regime = macro.get("regime", {})
    universe = get_universe("dividend", discover_dividend_universe, DIVIDEND_UNIVERSE_FALLBACK)
    payload = build_universe_payload(universe, lambda data: score_dividend(data, regime), rating_dividend, build_dividend_reason)
    etf_buckets = get_universe("etfs", discover_dividend_etfs, DIVIDEND_ETFS_FALLBACK)
    payload["etfs"] = {
        "monthly": build_etf_bucket(etf_buckets.get("monthly", [])),
        "quarterly": build_etf_bucket(etf_buckets.get("quarterly", [])),
        "annual": build_etf_bucket(etf_buckets.get("annual", [])),
    }
    payload["macro"] = macro
    return payload


# ---------------------------------------------------------------------------
# 4. ASSEMBLE PAYLOAD (main sector tool)
# ---------------------------------------------------------------------------
def build_payload():
    universe = get_universe("main", discover_sector_universe, SECTORS_FALLBACK)
    all_symbols = sorted({s for sub in universe.values() for lst in sub.values() for s in lst})
    fetched = fetch_all(all_symbols)

    sectors_out = {}
    call_sheet = []

    for sector, subindustries in universe.items():
        sector_symbols = [s for lst in subindustries.values() for s in lst]
        sector_data = [fetched[s] for s in sector_symbols if s in fetched]

        scores, avg_pe = score_sector(sector_data) if sector_data else ({}, None)

        sub_out = {}
        for sub, symbols in subindustries.items():
            rows = []
            for s in symbols:
                if s not in fetched:
                    continue
                d = fetched[s]
                sc = scores.get(s, {}).get("score", 0)
                rows.append({**d, "score": sc, "rating": rating_from_score(sc)})
            rows.sort(key=lambda r: r["score"], reverse=True)
            sub_out[sub] = rows

        if sector_data:
            best_symbol = max(scores, key=lambda s: scores[s]["score"])
            best = fetched[best_symbol]
            best_score = scores[best_symbol]["score"]
            best_rating = rating_from_score(best_score)
            reason = build_reason(best, avg_pe, best_rating)
            pick = {
                **best,
                "score": best_score,
                "rating": best_rating,
                "reason": reason,
                "breakdown": scores[best_symbol]["breakdown"],
            }
        else:
            pick = None

        sectors_out[sector] = {"subindustries": sub_out, "pick": pick, "sector_avg_pe": avg_pe}
        if pick:
            call_sheet.append({"sector": sector, **pick})

    return {
        "generated_at": time.time(),
        "sectors": sectors_out,
        "call_sheet": call_sheet,
    }


def get_cached(name, builder, ttl=DATA_TTL_SECONDS, force=False):
    with _cache_lock:
        entry = _caches.get(name)
        fresh = entry and (time.time() - entry["timestamp"]) < ttl
        if fresh and not force:
            return entry["payload"]
    try:
        payload = builder()
    except Exception as exc:
        # Belt-and-suspenders: every discover_*/build_* function already
        # guards its own network calls, but if something unexpected still
        # slips through, serve the last-known-good payload instead of a raw
        # 500 — a stale answer beats a broken page.
        print(f"[get_cached] '{name}' builder raised {exc!r} — serving stale/empty payload")
        with _cache_lock:
            stale = _caches.get(name)
        if stale:
            return stale["payload"]
        payload = {}
    with _cache_lock:
        _caches[name] = {"timestamp": time.time(), "payload": payload}
    return payload


# ---------------------------------------------------------------------------
# 6. PORTFOLIO WATCHLIST — persistent positions (SQLite, survives restarts),
#    live-priced on every read, with a proper money-weighted return (XIRR)
#    at the portfolio level rather than a naive average of percentages.
# ---------------------------------------------------------------------------
import sqlite3
import os
from datetime import datetime, date

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.db")
_db_lock = threading.Lock()


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Idempotent — safe to call on every request. Handles three states:
    1. Brand new DB: creates both tables fresh, with one default portfolio.
    2. Pre-multi-portfolio DB (a `positions` table with no `portfolio_id`
       column, from before this feature existed): adds the column via
       ALTER TABLE and backfills every existing row into a newly created
       "My Portfolio" — existing positions are preserved, never dropped.
    3. Already-migrated DB: everything is a no-op."""
    with _db_lock:
        conn = _get_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                portfolio_id INTEGER NOT NULL DEFAULT 1,
                symbol TEXT NOT NULL,
                quantity REAL NOT NULL,
                buy_price REAL NOT NULL,
                buy_date TEXT NOT NULL,
                sell_price REAL,
                sell_date TEXT,
                notes TEXT,
                created_at TEXT NOT NULL
            )
        """)

        cols = [r["name"] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        migrating_old_data = "portfolio_id" not in cols
        if migrating_old_data:
            conn.execute("ALTER TABLE positions ADD COLUMN portfolio_id INTEGER NOT NULL DEFAULT 1")

        portfolio_count = conn.execute("SELECT COUNT(*) AS n FROM portfolios").fetchone()["n"]
        if portfolio_count == 0:
            conn.execute(
                "INSERT INTO portfolios (id, name, created_at) VALUES (1, 'My Portfolio', ?)",
                (datetime.now().isoformat(),),
            )
            if migrating_old_data:
                print("[init_db] Migrated existing positions into a new default portfolio: 'My Portfolio'")

        conn.commit()
        conn.close()


# Run unconditionally at import time — NOT inside `if __name__ == "__main__"`,
# because a production WSGI server (gunicorn, waitress, etc.) imports this
# module without ever executing that block. Relying on it there would mean
# the table silently never gets created outside of `python app.py` directly.
init_db()


def _parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def xirr(cashflows):
    """Solve for the annualized rate r such that the net present value of
    all cashflows is zero — the standard money-weighted rate of return
    (same definition Excel's XIRR uses). cashflows: list of (date, amount)
    tuples; negative = money out (buys), positive = money in (sells / current
    mark-to-market value of still-open positions).

    Uses Newton-Raphson first (fast, precise), falling back to bisection if
    Newton doesn't converge (robust — always finds a root if one exists in
    the search bracket). Returns a percentage, or None if there isn't enough
    information (fewer than 2 distinct dates, or no sign change in the cash
    flows — e.g. everything is still an outflow)."""
    if len(cashflows) < 2:
        return None
    dates = [d for d, _ in cashflows]
    if max(dates) == min(dates):
        return None  # every cash flow on the same day — no time value to solve for
    t0 = min(dates)

    def npv(rate):
        total = 0.0
        for d, amt in cashflows:
            years = (d - t0).days / 365.0
            total += amt / ((1 + rate) ** years)
        return total

    def dnpv(rate):
        total = 0.0
        for d, amt in cashflows:
            years = (d - t0).days / 365.0
            if years == 0:
                continue
            total += -years * amt / ((1 + rate) ** (years + 1))
        return total

    # Newton-Raphson
    rate = 0.15
    for _ in range(100):
        try:
            f, fp = npv(rate), dnpv(rate)
        except (OverflowError, ZeroDivisionError):
            break
        if abs(fp) < 1e-12:
            break
        new_rate = rate - f / fp
        if not (-0.9999 < new_rate < 50):
            break
        if abs(new_rate - rate) < 1e-7:
            return round(new_rate * 100, 2)
        rate = new_rate

    # Bisection fallback — slower but guaranteed to converge if a root
    # exists between lo and hi (i.e. NPV changes sign across the bracket)
    lo, hi = -0.9999, 50.0
    try:
        f_lo, f_hi = npv(lo), npv(hi)
    except (OverflowError, ZeroDivisionError):
        return None
    if f_lo * f_hi > 0:
        return None  # no sign change in this bracket — can't solve reliably
    for _ in range(200):
        mid = (lo + hi) / 2
        try:
            f_mid = npv(mid)
        except (OverflowError, ZeroDivisionError):
            return None
        if abs(f_mid) < 1e-6:
            return round(mid * 100, 2)
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return round(((lo + hi) / 2) * 100, 2)


def compute_position_math(row, current_price):
    """All the per-position math. Returns None-safe fields throughout since
    a live price fetch can fail (delisted ticker, network hiccup, etc.)."""
    quantity = row["quantity"]
    buy_price = row["buy_price"]
    buy_date = _parse_date(row["buy_date"])
    cost_basis = quantity * buy_price
    is_closed = row["sell_price"] is not None and row["sell_date"] is not None

    result = {
        "id": row["id"], "symbol": row["symbol"], "quantity": quantity,
        "buy_price": buy_price, "buy_date": row["buy_date"],
        "sell_price": row["sell_price"], "sell_date": row["sell_date"],
        "notes": row["notes"], "cost_basis": round(cost_basis, 2),
        "current_price": current_price, "is_closed": is_closed,
    }

    if is_closed:
        sell_price = row["sell_price"]
        sell_date = _parse_date(row["sell_date"])
        exit_value = quantity * sell_price
        holding_days = max((sell_date - buy_date).days, 1)
        realized_pl = exit_value - cost_basis
        realized_pl_pct = (sell_price / buy_price - 1) * 100 if buy_price else None
        result.update({
            "exit_value": round(exit_value, 2),
            "holding_days": holding_days,
            "realized_gain": round(realized_pl, 2),
            "realized_gain_pct": round(realized_pl_pct, 2) if realized_pl_pct is not None else None,
            "unrealized_gain": None, "unrealized_gain_pct": None,
            "annualized_return_pct": _annualized(exit_value, cost_basis, holding_days),
        })
    else:
        today = date.today()
        holding_days = max((today - buy_date).days, 1)
        if current_price is not None:
            current_value = quantity * current_price
            unrealized_pl = current_value - cost_basis
            unrealized_pl_pct = (current_price / buy_price - 1) * 100 if buy_price else None
            result.update({
                "current_value": round(current_value, 2),
                "holding_days": holding_days,
                "unrealized_gain": round(unrealized_pl, 2),
                "unrealized_gain_pct": round(unrealized_pl_pct, 2) if unrealized_pl_pct is not None else None,
                "realized_gain": None, "realized_gain_pct": None,
                "annualized_return_pct": _annualized(current_value, cost_basis, holding_days),
            })
        else:
            result.update({
                "current_value": None, "holding_days": holding_days,
                "unrealized_gain": None, "unrealized_gain_pct": None,
                "realized_gain": None, "realized_gain_pct": None,
                "annualized_return_pct": None,
            })
    return result


def _annualized(end_value, cost_basis, holding_days):
    """CAGR-style annualized return for a SINGLE position. Deliberately
    suppressed under 7 days held — annualizing a 1-day move produces a huge,
    meaningless number (a 2% move in a day is not a +2,700%/year stock)."""
    if not cost_basis or cost_basis <= 0 or holding_days < 7:
        return None
    ratio = end_value / cost_basis
    if ratio <= 0:
        return -100.0
    try:
        return round((ratio ** (365.0 / holding_days) - 1) * 100, 2)
    except (OverflowError, ValueError):
        return None


def list_portfolios():
    with _db_lock:
        conn = _get_db()
        rows = conn.execute("""
            SELECT p.id, p.name, p.created_at, COUNT(pos.id) AS position_count
            FROM portfolios p
            LEFT JOIN positions pos ON pos.portfolio_id = p.id
            GROUP BY p.id
            ORDER BY p.id ASC
        """).fetchall()
        conn.close()
    return [dict(r) for r in rows]


def build_portfolio_payload(portfolio_id):
    init_db()
    with _db_lock:
        conn = _get_db()
        portfolio_row = conn.execute("SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)).fetchone()
        if portfolio_row is None:
            conn.close()
            return None
        rows = conn.execute(
            "SELECT * FROM positions WHERE portfolio_id = ? ORDER BY buy_date DESC, id DESC", (portfolio_id,)
        ).fetchall()
        conn.close()

    symbols = sorted({r["symbol"] for r in rows})
    prices = fetch_all(symbols) if symbols else {}

    positions = []
    for row in rows:
        d = prices.get(row["symbol"])
        current_price = d["price"] if d else None
        pos = compute_position_math(row, current_price)
        pos["name"] = d["name"] if d else row["symbol"]
        pos["pct_5d"] = d["pct_5d"] if d else None
        pos["sparkline"] = d["sparkline"] if d else None
        pos["companySector"] = d.get("companySector") if d else None
        positions.append(pos)

    open_positions = [p for p in positions if not p["is_closed"]]
    closed_positions = [p for p in positions if p["is_closed"]]

    total_cost_open = sum(p["cost_basis"] for p in open_positions)
    total_value_open = sum(p["current_value"] for p in open_positions if p["current_value"] is not None)
    total_unrealized = sum(p["unrealized_gain"] for p in open_positions if p["unrealized_gain"] is not None)

    total_cost_closed = sum(p["cost_basis"] for p in closed_positions)
    total_realized = sum(p["realized_gain"] for p in closed_positions if p["realized_gain"] is not None)

    # Money-weighted (not a naive average-of-percentages) simple return
    combined_cost = total_cost_open + total_cost_closed
    combined_pl = total_unrealized + total_realized
    simple_return_pct = round((combined_pl / combined_cost) * 100, 2) if combined_cost else None

    # Portfolio-level XIRR: true money-weighted, time-weighted annualized
    # return across every position's actual cash-flow dates
    cashflows = []
    for row in rows:
        buy_date = _parse_date(row["buy_date"])
        cashflows.append((buy_date, -(row["quantity"] * row["buy_price"])))
        if row["sell_price"] is not None and row["sell_date"] is not None:
            cashflows.append((_parse_date(row["sell_date"]), row["quantity"] * row["sell_price"]))
        else:
            d = prices.get(row["symbol"])
            if d:
                cashflows.append((date.today(), row["quantity"] * d["price"]))
    portfolio_xirr = xirr(cashflows)

    sector_allocation = {}
    for p in open_positions:
        if p["current_value"] is None:
            continue
        sec = p["companySector"] or "Unknown"
        sector_allocation[sec] = sector_allocation.get(sec, 0) + p["current_value"]

    for p in open_positions:
        p["allocation_pct"] = round(p["current_value"] / total_value_open * 100, 1) if (
            total_value_open and p["current_value"] is not None
        ) else None

    return {
        "generated_at": time.time(),
        "portfolio": {"id": portfolio_row["id"], "name": portfolio_row["name"]},
        "positions": positions,
        "summary": {
            "open_count": len(open_positions),
            "closed_count": len(closed_positions),
            "total_cost_open": round(total_cost_open, 2),
            "total_value_open": round(total_value_open, 2),
            "total_unrealized_gain": round(total_unrealized, 2),
            "total_unrealized_gain_pct": round(total_unrealized / total_cost_open * 100, 2) if total_cost_open else None,
            "total_cost_closed": round(total_cost_closed, 2),
            "total_realized_gain": round(total_realized, 2),
            "total_realized_gain_pct": round(total_realized / total_cost_closed * 100, 2) if total_cost_closed else None,
            "combined_gain": round(combined_pl, 2),
            "simple_return_pct": simple_return_pct,
            "portfolio_xirr_pct": portfolio_xirr,
            "sector_allocation": sector_allocation,
        },
    }



# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 7. TICKER NEWS — 1-2 recent real headlines per symbol held in a portfolio,
#    cached daily. These are Yahoo's actual headlines, not an AI-generated
#    summary (this app has no LLM wired in server-side) — a real headline is
#    already a one-line summary by nature, so this doesn't overclaim.
#
#    Yahoo's news JSON shape has changed across yfinance versions and isn't
#    independently verifiable from this environment (no live network access
#    here), so every field is extracted defensively with fallbacks, and the
#    whole thing degrades to an empty list rather than erroring.
# ---------------------------------------------------------------------------
NEWS_TTL_SECONDS = 24 * 60 * 60


def fetch_ticker_news(symbol, max_items=2):
    try:
        raw = yf.Ticker(symbol).get_news(count=max_items, tab="news") or []
    except Exception:
        return []

    out = []
    for item in raw[:max_items]:
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), dict) else item

        title = content.get("title") or item.get("title")
        if not title:
            continue

        provider = content.get("provider")
        publisher = (
            provider.get("displayName") if isinstance(provider, dict) else None
        ) or item.get("publisher") or content.get("publisher") or "Unknown source"

        canonical = content.get("canonicalUrl")
        link = (
            canonical.get("url") if isinstance(canonical, dict) else None
        ) or item.get("link") or content.get("link")

        published = content.get("pubDate") or content.get("displayTime") or item.get("providerPublishTime")

        out.append({
            "symbol": symbol,
            "title": str(title).strip(),
            "publisher": publisher,
            "link": link,
            "published": published,
        })
    return out


def build_portfolio_news(symbols, max_items_per_symbol=2):
    if not symbols:
        return {}
    news_by_symbol = {}
    with ThreadPoolExecutor(max_workers=min(16, len(symbols))) as pool:
        futures = {pool.submit(fetch_ticker_news, sym, max_items_per_symbol): sym for sym in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            news_by_symbol[sym] = _safe_result(fut, [])
    return news_by_symbol


# ---------------------------------------------------------------------------
# 8. STOCK SEARCH + QUOTE DETAIL (search bar → modal popup)
# ---------------------------------------------------------------------------
def search_tickers(query, max_results=8):
    try:
        results = yf.Search(query, max_results=max_results, news_count=0, lists_count=0,
                             include_cb=False, enable_fuzzy_query=True, raise_errors=False).quotes
    except Exception:
        return []
    out = []
    for q in results or []:
        symbol = q.get("symbol")
        if not symbol:
            continue
        out.append({
            "symbol": symbol,
            "name": q.get("shortname") or q.get("longname") or symbol,
            "exchange": q.get("exchDisp") or q.get("exchange"),
            "type": q.get("typeDisp") or q.get("quoteType"),
        })
    return out


def get_quote_detail(symbol):
    base = fetch_ticker(symbol)
    if not base:
        return None
    extra = fetch_etf_returns(symbol) or {}
    return {**base, **extra}


# ---------------------------------------------------------------------------
# 9. ROUTES
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/portfolio")
def portfolio_page():
    return render_template("portfolio.html")


@app.route("/api/data")
def api_data():
    return jsonify(get_cached("main", build_payload, force=False))


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    return jsonify(get_cached("main", build_payload, force=True))


@app.route("/api/penny")
def api_penny():
    return jsonify(get_cached("penny", build_penny_payload, force=False))


@app.route("/api/penny/refresh", methods=["POST"])
def api_penny_refresh():
    return jsonify(get_cached("penny", build_penny_payload, force=True))


@app.route("/api/dividend")
def api_dividend():
    return jsonify(get_cached("dividend", build_dividend_payload, force=False))


@app.route("/api/dividend/refresh", methods=["POST"])
def api_dividend_refresh():
    return jsonify(get_cached("dividend", build_dividend_payload, force=True))


@app.route("/api/etf-families")
def api_etf_families():
    return jsonify(get_cached("etf_families", build_etf_families_payload, ttl=DATA_TTL_SECONDS))


@app.route("/api/etf-families/refresh", methods=["POST"])
def api_etf_families_refresh():
    return jsonify(get_cached("etf_families", build_etf_families_payload, ttl=DATA_TTL_SECONDS, force=True))


@app.route("/api/macro")
def api_macro():
    return jsonify(get_cached("macro", build_macro_payload, ttl=DATA_TTL_SECONDS))


@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    """Force a full re-discovery of every universe (sectors, penny, dividend
    stocks, dividend ETFs) — not just a price refresh. This is the 'ask
    Yahoo again, from scratch, what qualifies today' button."""
    get_cached("universe_main", discover_sector_universe, ttl=UNIVERSE_TTL_SECONDS, force=True)
    get_cached("universe_penny", discover_penny_universe, ttl=UNIVERSE_TTL_SECONDS, force=True)
    get_cached("universe_dividend", discover_dividend_universe, ttl=UNIVERSE_TTL_SECONDS, force=True)
    get_cached("universe_etfs", discover_dividend_etfs, ttl=UNIVERSE_TTL_SECONDS, force=True)
    return jsonify({
        "main": get_cached("main", build_payload, force=True),
        "penny": get_cached("penny", build_penny_payload, force=True),
        "dividend": get_cached("dividend", build_dividend_payload, force=True),
    })


def _invalidate_portfolio_caches(pid):
    with _cache_lock:
        _caches.pop(f"portfolio_{pid}", None)
        _caches.pop(f"news_{pid}", None)


@app.route("/api/portfolios")
def api_portfolios_list():
    init_db()
    return jsonify(list_portfolios())


@app.route("/api/portfolios", methods=["POST"])
def api_portfolios_create():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    with _db_lock:
        conn = _get_db()
        cur = conn.execute("INSERT INTO portfolios (name, created_at) VALUES (?, ?)", (name, datetime.now().isoformat()))
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
    return jsonify({"id": new_id, "name": name, "position_count": 0})


@app.route("/api/portfolios/<int:pid>", methods=["PUT"])
def api_portfolios_rename(pid):
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    with _db_lock:
        conn = _get_db()
        existing = conn.execute("SELECT id FROM portfolios WHERE id = ?", (pid,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({"error": f"No portfolio with id {pid}"}), 404
        conn.execute("UPDATE portfolios SET name = ? WHERE id = ?", (name, pid))
        conn.commit()
        conn.close()
    _invalidate_portfolio_caches(pid)
    return jsonify(list_portfolios())


@app.route("/api/portfolios/<int:pid>", methods=["DELETE"])
def api_portfolios_delete(pid):
    """Deletes the portfolio AND its positions. Always leaves at least one
    portfolio behind — deleting the last remaining one immediately recreates
    a fresh empty 'My Portfolio' rather than leaving the app in a dead end."""
    with _db_lock:
        conn = _get_db()
        conn.execute("DELETE FROM positions WHERE portfolio_id = ?", (pid,))
        conn.execute("DELETE FROM portfolios WHERE id = ?", (pid,))
        remaining = conn.execute("SELECT COUNT(*) AS n FROM portfolios").fetchone()["n"]
        if remaining == 0:
            conn.execute(
                "INSERT INTO portfolios (name, created_at) VALUES (?, ?)",
                ("My Portfolio", datetime.now().isoformat()),
            )
        conn.commit()
        conn.close()
    _invalidate_portfolio_caches(pid)
    return jsonify(list_portfolios())


@app.route("/api/portfolios/<int:pid>/clear", methods=["POST"])
def api_portfolios_clear(pid):
    """Deletes every position in this ONE portfolio, keeping the portfolio
    itself (now empty)."""
    with _db_lock:
        conn = _get_db()
        existing = conn.execute("SELECT id FROM portfolios WHERE id = ?", (pid,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({"error": f"No portfolio with id {pid}"}), 404
        conn.execute("DELETE FROM positions WHERE portfolio_id = ?", (pid,))
        conn.commit()
        conn.close()
    _invalidate_portfolio_caches(pid)
    return jsonify(get_cached(f"portfolio_{pid}", lambda: build_portfolio_payload(pid), ttl=300, force=True))


@app.route("/api/portfolios/clear-all", methods=["POST"])
def api_portfolios_clear_all():
    """The nuclear option: wipes EVERY portfolio and EVERY position across
    the whole app, then recreates one fresh empty default portfolio."""
    with _db_lock:
        conn = _get_db()
        conn.execute("DELETE FROM positions")
        conn.execute("DELETE FROM portfolios")
        conn.execute(
            "INSERT INTO portfolios (id, name, created_at) VALUES (1, 'My Portfolio', ?)",
            (datetime.now().isoformat(),),
        )
        conn.commit()
        conn.close()
    with _cache_lock:
        for key in [k for k in _caches if k.startswith("portfolio_") or k.startswith("news_")]:
            del _caches[key]
    return jsonify(list_portfolios())


@app.route("/api/portfolios/<int:pid>")
def api_portfolio_get(pid):
    payload = get_cached(f"portfolio_{pid}", lambda: build_portfolio_payload(pid), ttl=300)
    if payload is None:
        return jsonify({"error": f"No portfolio with id {pid}"}), 404
    return jsonify(payload)


@app.route("/api/portfolios/<int:pid>/refresh", methods=["POST"])
def api_portfolio_refresh(pid):
    payload = get_cached(f"portfolio_{pid}", lambda: build_portfolio_payload(pid), ttl=300, force=True)
    if payload is None:
        return jsonify({"error": f"No portfolio with id {pid}"}), 404
    return jsonify(payload)


@app.route("/api/portfolios/<int:pid>/positions", methods=["POST"])
def api_position_add(pid):
    body = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    try:
        quantity = float(body.get("quantity"))
        buy_price = float(body.get("buy_price"))
        buy_date = body.get("buy_date")
        datetime.strptime(buy_date, "%Y-%m-%d")  # validate format
    except (TypeError, ValueError):
        return jsonify({"error": "symbol, quantity, buy_price, and buy_date (YYYY-MM-DD) are required"}), 400
    if not symbol or quantity <= 0 or buy_price <= 0:
        return jsonify({"error": "symbol, quantity, and buy_price must be positive"}), 400

    sell_price, sell_date = body.get("sell_price"), body.get("sell_date")
    if (sell_price is None) != (sell_date is None):
        return jsonify({"error": "sell_price and sell_date must be provided together, or both left blank"}), 400
    if sell_date is not None:
        try:
            datetime.strptime(sell_date, "%Y-%m-%d")
            sell_price = float(sell_price)
        except (TypeError, ValueError):
            return jsonify({"error": "sell_price must be a number and sell_date must be YYYY-MM-DD"}), 400
        if sell_date < buy_date:
            return jsonify({"error": "sell_date can't be before buy_date"}), 400

    with _db_lock:
        conn = _get_db()
        portfolio_exists = conn.execute("SELECT id FROM portfolios WHERE id = ?", (pid,)).fetchone()
        if not portfolio_exists:
            conn.close()
            return jsonify({"error": f"No portfolio with id {pid}"}), 404
        conn.execute(
            "INSERT INTO positions (portfolio_id, symbol, quantity, buy_price, buy_date, sell_price, sell_date, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, symbol, quantity, buy_price, buy_date, sell_price, sell_date, body.get("notes"), datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
    _invalidate_portfolio_caches(pid)
    return jsonify(get_cached(f"portfolio_{pid}", lambda: build_portfolio_payload(pid), ttl=300, force=True))


@app.route("/api/portfolios/<int:pid>/positions/<int:position_id>", methods=["PUT"])
def api_position_update(pid, position_id):
    body = request.get_json(force=True, silent=True) or {}

    with _db_lock:
        conn = _get_db()
        existing = conn.execute(
            "SELECT * FROM positions WHERE id = ? AND portfolio_id = ?", (position_id, pid)
        ).fetchone()
        if not existing:
            conn.close()
            return jsonify({"error": f"No position with id {position_id} in portfolio {pid}"}), 404

        buy_date = body.get("buy_date", existing["buy_date"])
        sell_date = body["sell_date"] if "sell_date" in body else existing["sell_date"]
        sell_price = body["sell_price"] if "sell_price" in body else existing["sell_price"]
        quantity = body.get("quantity", existing["quantity"])
        buy_price = body.get("buy_price", existing["buy_price"])

        try:
            if quantity is not None and float(quantity) <= 0:
                raise ValueError
            if buy_price is not None and float(buy_price) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            conn.close()
            return jsonify({"error": "quantity and buy_price must be positive numbers"}), 400

        if (sell_price is None) != (sell_date is None):
            conn.close()
            return jsonify({"error": "sell_price and sell_date must be provided together, or both left blank"}), 400
        if sell_date is not None and sell_date < buy_date:
            conn.close()
            return jsonify({"error": "sell_date can't be before buy_date"}), 400

        fields, values = [], []
        for key in ["quantity", "buy_price", "buy_date", "sell_price", "sell_date", "notes"]:
            if key in body:
                fields.append(f"{key} = ?")
                values.append(body[key])
        if not fields:
            conn.close()
            return jsonify({"error": "no fields to update"}), 400
        values.append(position_id)
        conn.execute(f"UPDATE positions SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        conn.close()
    _invalidate_portfolio_caches(pid)
    return jsonify(get_cached(f"portfolio_{pid}", lambda: build_portfolio_payload(pid), ttl=300, force=True))


@app.route("/api/portfolios/<int:pid>/positions/<int:position_id>", methods=["DELETE"])
def api_position_delete(pid, position_id):
    with _db_lock:
        conn = _get_db()
        conn.execute("DELETE FROM positions WHERE id = ? AND portfolio_id = ?", (position_id, pid))
        conn.commit()
        conn.close()
    _invalidate_portfolio_caches(pid)
    return jsonify(get_cached(f"portfolio_{pid}", lambda: build_portfolio_payload(pid), ttl=300, force=True))


@app.route("/api/portfolios/<int:pid>/news")
def api_portfolio_news(pid):
    with _db_lock:
        conn = _get_db()
        rows = conn.execute("SELECT DISTINCT symbol FROM positions WHERE portfolio_id = ?", (pid,)).fetchall()
        conn.close()
    symbols = sorted({r["symbol"] for r in rows})
    payload = get_cached(f"news_{pid}", lambda: build_portfolio_news(symbols), ttl=NEWS_TTL_SECONDS)
    return jsonify(payload)


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if len(q) < 1:
        return jsonify([])
    return jsonify(search_tickers(q))


@app.route("/api/quote/<symbol>")
def api_quote(symbol):
    detail = get_cached(f"quote_{symbol.upper()}", lambda: get_quote_detail(symbol.upper()), ttl=300)
    if not detail:
        return jsonify({"error": f"No data found for '{symbol}'"}), 404
    return jsonify(detail)


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000, threaded=True)
