[![Python CI](https://github.com/habibjubair/stock-terminal/actions/workflows/python.yml/badge.svg)](https://github.com/habibjubair/stock-terminal/actions/workflows/python.yml)

# Sector Call Sheet - US Stock Terminal 

A small local web terminal that scans US markets live via Yahoo Finance and surfaces
the strongest name in each sector/theme/income bucket — with a plain-English reason
you can read straight and understand.

**Nothing is a fixed stock list.** Every universe — which companies belong to which
sector, which sub-$5 names are actually moving today, which stocks are actually
yielding well right now, which ETFs qualify as high-yield, and even whether an ETF
pays monthly/quarterly/annually — is asked of Yahoo Finance live and re-discovered
once a day (cached 24h so you're not hammering Yahoo's rate limits; hit **Rescan
Universe** to force it sooner). Only the *prices and fundamentals* for those
discovered tickers refresh more often (every 20 min, or on demand via **Refresh
Prices**).

## Setup

```bash
cd stock-terminal
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000** for the sector/penny/dividend terminal, or
**http://127.0.0.1:5000/portfolio** for the Portfolio Watchlist. Both pages share
the same search bar (top of the page) for looking up any ticker in a detail modal.
First load of the main terminal takes 30–90 seconds — it's live-discovering
~150-250 tickers across sectors, industries, momentum screens, dividend
screens, and ETF categories, then pulling price/fundamentals for all of them.

## How the discovery works

| Tab | How tickers are found, live | Nothing hardcoded |
|---|---|---|
| Sector Leaders | `yf.Sector(key).industries` → top industries by market weight, then `yf.Industry(key).top_companies` → top companies per industry | Sector *and* sub-industry membership come straight from Yahoo |
| Penny Stock Radar | Custom `yf.EquityQuery`: US exchanges only, price < $5, liquidity floor (avg 3-month volume), split into "Today's Momentum Gainers" (% change) and "Today's Unusual Volume" (volume vs. its own average) | Grouped by today's actual technical pattern, not a fixed theme list that goes stale |
| Dividend & Income | Custom `yf.EquityQuery` per sector: US exchanges, forward dividend yield above a floor, sorted by yield | "Which stocks are yielding well" is asked fresh, not assumed |
| Dividend ETFs | Custom `yf.ETFQuery` over income-oriented fund categories, then each candidate's **real dividend history** (`yf.Ticker(x).dividends`) is used to count actual distributions in the last ~14 months and classify it as monthly/quarterly/annual | Frequency bucket is measured, not assumed |

If a live Yahoo call fails (rate limit, no network, schema hiccup), each discovery
step falls back to a small built-in starter list rather than showing a blank page —
but in normal operation this is never used.

## New: Multiple named portfolios (it's really a watchlist)

The Portfolio page now supports **any number of portfolios, each with its own
name** — a dropdown selector at the top switches between them, remembers your last
choice (`localStorage`), and every position, summary calculation, and news feed is
fully isolated per portfolio (verified — adding a position to one never leaks into
another).

- **+ New Portfolio** — name it anything ("Client - Retirement Account", "Speculative
  Watchlist", whatever fits your workflow).
- **Rename** — rename the currently selected one.
- **Delete Portfolio** — removes it and its positions. Deleting your *last* remaining
  portfolio doesn't leave you stranded — it immediately creates a fresh empty "My
  Portfolio" in its place.
- **Clear** — deletes every position in the *current* portfolio but keeps the
  portfolio itself (now empty). Confirms before running.
- **Clear History ⚠️** — the nuclear option: wipes **every portfolio and every
  position in the entire app**. Requires typing "DELETE" to confirm, given how
  destructive it is.
- **Export CSV** — downloads the current portfolio's positions (all the math columns
  included) as a spreadsheet-ready CSV, generated client-side, no server round trip.

**If you already had positions saved** from before this update: the database
migration is automatic and non-destructive — your existing `positions` table gets a
new `portfolio_id` column added, and every existing position is backfilled into a
newly created "My Portfolio" the first time you run the updated app. Verified against
a simulated pre-migration database with real data before shipping this.

## New: Ticker news (per-holding, auto-refreshed daily)

A "Recent News" section on the Portfolio page shows 1–2 recent headlines for every
ticker you currently hold, grouped by symbol, each linking out to the source.
**Important honesty note:** these are Yahoo's actual real headlines, not an
AI-generated summary — this app has no LLM wired in server-side, and a real headline
is already a one-line summary by nature, so nothing here is oversold as "AI
intelligence" it doesn't have. News is cached for 24h per portfolio and automatically
invalidated (re-fetched) whenever you add or remove a position, so it never goes
stale relative to what you're actually holding. One caveat: Yahoo's underlying news
JSON structure isn't something this sandbox could verify against live data while
building it, so the field-extraction is written defensively (tries several known key
paths, degrades to an empty list rather than erroring) — if headlines come back empty
on your first real run, that's the signal the schema shifted and the field names in
`fetch_ticker_news()` need a small update.

## New: Auto-refresh (5s / 10s / 1m / 15m)

A small dropdown next to "Refresh Prices" on both pages — pick an interval and the
page keeps itself current on a timer, with a pulsing dot while active. A few
deliberate design choices:
- **Pauses automatically when the browser tab isn't visible** (Page Visibility API)
  and catches up immediately the moment you switch back — no wasted requests against
  Yahoo while you're not even looking at the screen.
- **Refreshes prices only**, never triggers a full universe rescan — auto-refresh on
  the main terminal calls the same lightweight "Refresh Prices" path as the manual
  button, not "Rescan Universe."
- **Fast intervals (5s/10s) show a rate-limit warning in the tooltip.** They're
  genuinely fine on the Portfolio page (usually a handful of tickers), but the main
  terminal's sector/penny/dividend tabs can carry 150+ tickers — hammering that every
  5 seconds risks getting rate-limited by Yahoo. Nothing is blocked, just flagged.
- Your choice is remembered per-page across reloads.

## New: Monthly Gainers ladder (fully dynamic, no price ceiling)

On the Penny Stock Radar tab, `discover_monthly_gainers_ladder()` replaces the old
single 300%+ group with six live, exclusive tiers — **10–30%, 30–50%, 50–70%,
70–100%, 100–200%, 200%+** — each stock sorted into the ONE tier that actually
describes its real trailing 1-month return. Since Yahoo's screener has no native
"% change over 1 month" field, this works in two steps, same pattern as the ETF
frequency classification: cast a broad net (today's biggest gainers + biggest
52-week movers, major US exchanges, a liquidity floor, no price cap), then
**compute** each candidate's actual 1-month return from real price history and
bucket it. Nothing is a fixed "hot stocks" list — every run re-asks Yahoo and
re-measures.

## New: Portfolio Watchlist page (`/portfolio`)

A second page with its own nav link, backed by a local SQLite database
(`portfolio.db`, created automatically next to `app.py`) so positions survive
restarts. Track: **quantity, buy price, buy date**, live **current price**, and —
once you fill in a **sell price** and **sell date** — the position automatically
switches from showing an **Unrealized Gain** to a **Realized Gain**.

**The math** (`compute_position_math`, `xirr` in `app.py`):
- Cost basis, current/exit value, and $ / % gain — realized OR unrealized,
  whichever applies, computed fresh on every load using a live price fetch.
- **Annualized return per position** (CAGR-style), deliberately suppressed for
  holding periods under 7 days — annualizing a 1-day move produces a huge,
  meaningless number, not a realistic rate. Between 7–30 days it's shown but
  flagged with `*` as a "short holding period — extrapolation, not a forecast."
- **Portfolio-level XIRR** — the actual money-weighted, time-weighted annualized
  return across every position's real cash-flow dates (same definition as Excel's
  XIRR: solve for the rate that zeroes out the NPV of every buy/sell, with open
  positions marked-to-market as if liquidated today). Implemented as a from-scratch
  Newton-Raphson solver with a bisection fallback for robustness — no external
  finance library needed. This is deliberately NOT a naive average of each
  position's individual % return, which double-counts compounding and ignores
  position size and timing; it's verified against known analytical cases (e.g. a
  clean 2x over exactly 2 years solves to ~41.4%, the correct √2−1).
- **Sector allocation** (open positions only, live `companySector` from Yahoo).
- Every write (add/edit/close/delete) validates: quantity and buy price must be
  positive, sell price and sell date must be provided together (or both left
  blank), and sell date can't be before buy date.

## New: Global stock search → modal popup

A search bar in the top bar of both pages (`yf.Search` under the hood) with live
typeahead suggestions; picking a result (or pressing Enter) opens a modal with a
full fundamental + technical snapshot — price/5-day/1-month/3-month/YTD returns,
52-week range, beta, volume, P/E (trailing & forward), revenue/earnings growth,
margins, ROE, current ratio, market cap, dividend yield, payout ratio, and analyst
consensus/target — all pulled live via `/api/quote/<symbol>`.

## Bug fix: /api/dividend was throwing a 500

**Root cause:** the ETF discovery query used `categoryname` values ("Equity Income",
"Derivative Income") that don't actually exist in yfinance's accepted category list —
the real Morningstar category for covered-call/derivative-income funds is **"Option
Writing"**. That raised a `ValueError` while *constructing* the query, before it ever
reached the network, and nothing was catching it — so a single bad string crashed the
entire request even though the sector/stock parts had already succeeded.

**Fixed two ways:**
1. Corrected the category list to valid values: `["Option Writing", "Large Value",
   "Preferred Stock", "Real Estate"]`.
2. Closed the actual gap that let this happen: every query is now built inside a
   zero-arg function passed to `_run_screen`, so construction errors are caught by the
   exact same `try/except` as the network call. Every `ThreadPoolExecutor` result is
   now gathered through a `_safe_result()` helper that swallows a single worker's
   exception instead of letting it propagate and kill the whole batch. And
   `get_cached()` now catches anything that still slips through and serves the
   last-known-good payload instead of a raw 500. One bad ticker, field, or query
   should no longer be able to take down a whole tab.

## New: 300%+ Monthly Breakout screen

Added to the Penny Stock Radar tab. Yahoo's screener has no native "% change over 1
month" field, so this works in two steps: cast a broad net (today's biggest gainers +
biggest 52-week movers, major US exchanges, a liquidity floor, **no price ceiling** —
an explosive mover isn't necessarily still under $5), then **compute** each
candidate's real trailing 1-month price return from actual price history and keep
only the ones that genuinely cleared 300%. Nothing here is assumed from a category or
tag — it's measured per candidate, same pattern as the ETF payout-frequency
classification. Rated on the same Hot/Building/Neutral/Cooling scale, with the real
1-month and 3-month return shown in place of the volume/range columns used by the
other penny groups.

## What's new: performance fix + ETF Families

**Fixed:** the Dividend & Income tab could hang indefinitely. Root cause: several
discovery steps were making dozens of Yahoo calls **sequentially** (e.g. checking up
to 60 candidate ETFs' dividend history one at a time, or screening 8 sectors one at a
time). Every discovery step now runs in a parallel thread pool, and the Flask dev
server runs with `threaded=True` so one slow tab can't block the others.

**Added:** a fourth section on the Dividend & Income tab — **High-Yield /
Option-Income ETF Families** — covering the specific named issuer families you asked
for (`ETF_FAMILIES` in `app.py`):

| Family | Tickers |
|---|---|
| YieldMax — Mega-Cap Tech | APLY, AMZY, GOOY, FBY, MSFO, NFLY, DISO |
| YieldMax — AI & Semiconductors | AIYY, AMDY, INYY, SMCY, NVDY, SNOY, MRNY |
| YieldMax — Crypto | MSTY, WNTR, CONY, FIAT, MARO |
| YieldMax — Consumer / Internet | PYPY, SQY, ABNY, RDYY, RBLY, CVNY, HIYY |
| YieldMax — Auto | TSLY, CRSH |
| YieldMax — Other | ULTY, CRCO |
| GraniteShares YieldBOOST — Technology | AMYY, HMYY, MUYY, IOYY, SMYY, RGYY, QBY, CWY, SEMY |
| GraniteShares YieldBOOST — Crypto | MAAY, RTYY, CRY |
| Roundhill — WeeklyPay | ARMW, AMDW, HOOW |
| Roundhill — Covered Call | YETH |
| ARK 21Shares | ARKA, ARKC, ARKY |
| Defiance ETFs | SOUX, QQQY, JEPY, IWMY, SPYT |
| JPMorgan Income ETFs | JEPI, JEPQ, JPIE |
| Global X Covered Call Family | QYLD, XYLD, RYLD, QCLR, XRMI |
| NEOS Income ETFs | SPYI, QQQI, IWMI |
| RexShares / MicroStrategy Theme | MSTY, MSTX, MSTU |
| Crypto & Option-Income ETFs | YETH, CONY, MSTY, FIAT, MARO, BEGS |
| Monthly Income — Conservative | JEPI, JEPQ, SPYI, QQQI |
| Monthly Income — Aggressive | QYLD, XYLD, RYLD, IWMY, JEPY, QQQY |
| Monthly Income — Ultra-High Yield | MSTY, CONY, NVDY, TSLY, ULTY, SMCY, MARO, FIAT, WNTR, CRCO |
| Monthly Income — Crypto-Oriented | MSTY, CONY, MARO, MAAY, RTYY, ARKA, ARKC, ARKY |

Unlike the rest of the app, this ticker-to-family mapping is intentionally **static**
(it's a specific named-product directory, not something a generic screener query
reliably reconstructs) — but every number shown for them is still 100% live: price,
yield, expense ratio, net assets, and 1-month/3-month/YTD total price return
(computed from real price history, not assumed).

Each family is rated **Strong Buy / Buy / Hold / Sell**, scored on:
- Distribution yield (35%)
- Expense ratio (15%, lower is better)
- **Total return — YTD, or 3-month if YTD isn't available (40%, the largest single
  factor)** — this is deliberate: a fund can carry a huge headline yield while its
  price quietly erodes faster than the payout, so total return is weighted above raw
  yield to separate genuine income value from a yield trap. Any fund with a big yield
  and a sharply negative YTD return gets an explicit "NAV erosion" caution in its
  reason text.
- 5-day momentum (10%)

Each family also carries a static **risk tier** (Moderate / Medium / High / Very
High) based on product structure — single-stock and leveraged crypto-linked funds are
tagged Very High regardless of their current score, since concentration and NAV
erosion risk don't show up in a single day's numbers.

**Note on "analyst rating":** ETFs, especially this niche of option-income products,
generally don't have real sell-side analyst coverage the way stocks do. Rather than
fabricate one, the tool shows its own composite **Rating** (Strong Buy/Buy/Hold/Sell)
clearly labeled as that — not a stand-in for real analyst coverage.

**Suitability flag:** these products are a meaningfully different risk profile than
the broad dividend ETFs above them — built for the ones explicitly chasing high
current income, not for a conservative, capital-preservation-focused retiree. Worth
weighing that before using this section with the "already invested, stability-
seeking" persona from the Dividend & Income tab's original design.



## The three tabs

### 1. Sector Leaders
Scores every discovered ticker **within its own sector** (0–100) on: valuation
(P/E vs. peers), revenue growth, earnings growth, profit margin, ROE, current ratio
(liquidity/solvency), analyst consensus, price-target upside, and 5-day momentum.
Rating: **Strong Buy / Buy / Hold / Sell**.

### 2. Penny Stock Radar — for younger/first-time YOU, short-term stories
Purely **short-term technical** scoring: 5-day momentum, volume vs. its own average
(catches unusual activity), and where price sits in its 52-week range — deliberately
not a fundamentals score, since most of these names don't have mature fundamentals to
score. Ratings use different language on purpose — **Hot / Building / Neutral /
Cooling** — so this never reads as an investment recommendation. Every card carries a
built-in risk line (liquidity, volatility, "size and stop it").

### 3. Dividend & Income Desk — for retired/already-invested,
Scores **yield quality** (peaks around ~5.5% — a double-digit headline yield actually
scores worse, since that's usually a distress signal), payout-ratio sustainability,
beta stability, and 5-day capital-value momentum. Rating: **Strong Income Pick /
Income Pick / Watch / Caution**.

Three **High-Yield Dividend ETF** tables, one per real payout frequency, each rated
**Strong Buy / Buy / Hold / Sell** on the same scale as Sector Leaders (scored within
its own bucket on yield, expense ratio, 3-year average return, and momentum):
- **Monthly** — covered-call & super-high-yield funds
- **Quarterly** — classic quality/high-yield dividend-equity funds (deepest, most liquid)
- **Annual** — genuinely rare in the US high-yield ETF space; the true annual-only
  distributors that exist are modest-yield index/international funds, included for
  completeness, not for pitching yield

## Macro snapshot & regime tilt

A small live macro strip (10-year Treasury yield, VIX, US Dollar Index, S&P 500) runs
across the top of every tab — useful context to bring up on a call, and it also
lightly, transparently tilts scoring:
- **VIX ≥ 20 (risk-off):** Penny Radar shifts weight off raw momentum and onto
  confirmed volume, since momentum is less trustworthy in a choppy tape.
- **10-year yield rising >1.5% over 5 days:** Dividend & Income shifts weight off
  headline yield and onto payout sustainability and low beta, since rate-sensitive,
  high-payout names come under more pressure in that environment.

Every tilt is stated in the macro note under the strip — never a silent adjustment.

## Refresh vs. Rescan

- **Refresh Prices** — re-pulls price/fundamentals for the *already-discovered*
  tickers (fast, ~20-min cache).
- **Rescan Universe** — forces Yahoo to re-run every discovery query from scratch:
  which companies lead each sector today, what's moving under $5 right now, what's
  yielding well right now, which ETFs qualify and at what frequency. Runs
  automatically once every 24h; use the button to force it sooner.

## Editing thresholds

Everything tunable lives near the top of `app.py`:
- `SECTOR_KEYS` — the 11 Yahoo sector keys behind Sector Leaders
- `SCREENER_SECTOR_NAMES` — the 8 sectors screened for Dividend & Income
- `MAIN_US_EXCHANGES` — which exchanges count as "major US" (Pink/OTC excluded)
- `discover_penny_universe()` — `max_price`, `min_avg_volume` (liquidity floor)
- `discover_dividend_universe()` — `min_yield_pct`
- `discover_dividend_etfs()` — the fund categories screened, and the distribution-
  count thresholds used to classify monthly/quarterly/annual
- Scoring weights and rating thresholds live in `score_sector` / `score_penny` /
  `score_dividend` / `score_etf` and their matching `rating_*` functions
- `UNIVERSE_TTL_SECONDS` / `DATA_TTL_SECONDS` — how often discovery vs. price data refreshes

## Important notes

- **This is a screening tool, not investment advice**, on every tab. It mechanically
  ranks live public data — it doesn't know about news, litigation, guidance changes,
  or your firm's actual research. 
  This is to give you a jump-start of the strongest opportunity within the sectors. 
- Yahoo's screener/sector endpoints are unofficial and can occasionally change shape
  or rate-limit you; the fallback lists exist so the app degrades gracefully rather
  than breaking, but if you see a lot of "No data — try Refresh", wait a minute and
  hit Rescan Universe again.
- Every ticker across all tabs is restricted to major US exchanges (Nasdaq, NYSE,
  NYSE American) — no OTC/Pink Sheets, no international listings.
