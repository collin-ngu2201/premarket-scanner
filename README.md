# US Pre-Market Scanner

A standalone web app that scans the **S&P Composite 1500** (S&P 500 + 400 + 600,
~1,500 large/mid/small-cap names) for pre-market movers and lets you filter by
volume, market cap, sector, gainers/losers, and deviation from the main indexes
(SPY / QQQ). Deployed as a static site + serverless functions on **Vercel**.
(Sibling tabs: Divergence, Options / IV-rank CSP screener, Key Levels, and
**Money Flow** — accumulation vs distribution from signed volume.)

## What it does

- **Live scan** of the universe every 60s (toggleable), with a manual refresh.
- **Filters:** search, change basis (auto / pre / regular / post), direction
  (all / gainers / losers), min |move| %, min price, min volume, min rel-vol,
  market-cap buckets (mega → micro), and sector.
- **Deviation vs index:** each name's move minus SPY's (or QQQ's) move, with a
  `min |deviation|` filter — flags relative strength/weakness vs the market.
- **Leaderboards:** top gainers, top losers, most active by volume, biggest
  deviation vs SPY. Click any row/leaderboard entry to focus it.
- **Sortable table** on every column.

US pre-market runs **4:00–9:30am ET ≈ 4:00–9:30pm SGT**, so this is live during
your evening. When the market is closed the app shows the last regular session.

## Architecture

```
index.html                     SPA: universe load, scan math, filters, table (vanilla JS)
flow.html                      Money Flow dashboard (accumulation vs distribution)
universe.json                  S&P 1500 constituents + GICS sector (~1,500 names)
sample_quotes.json             snapshot fallback (written by tools/probe.py)
api/quotes.mjs                 batched quote proxy → /api/quotes (Vercel function)
api/chart.mjs                  OHLCV proxy → /api/chart (candles [t,o,h,l,c,v])
tools/probe.py                 local validator + snapshot generator (no Node needed)
tools/flow_lib.py              order-flow math (signed volume, CVD, VPIN, profile…)
tools/scan_flow.py             money-flow scanner → flow_results.json
tools/test_flow_lib.py         self-tests for the flow math (no network needed)
indicators/flow_delta.pine     the same method as a TradingView indicator
```

**Data flow:** the browser loads `universe.json`, POSTs the symbol list (plus
SPY/QQQ/DIA) to `/api/quotes`, and does all filtering/ranking client-side. The
function fetches Yahoo Finance in batches of 100, handling the cookie + crumb
auth that a browser can't do cross-origin. If `/api/quotes` is unavailable
(static preview, weekend, or a Yahoo hiccup) the app falls back to
`sample_quotes.json` and labels the data as a snapshot.

### Provider abstraction (free now, upgrade-ready)

`quotes.mjs` normalizes every provider to one quote shape, so the front-end is
provider-agnostic. Today it uses **Yahoo** (free, keyless). To move to a paid
**full-universe** feed later, implement `polygonQuotes()` (stub included), set
`POLYGON_KEY` in the Vercel project env, and call `/api/quotes?provider=polygon`.
No front-end changes needed.

## Money Flow — are traders injecting or escaping?

`flow.html` (backend: `tools/scan_flow.py`) exists to answer the one thing a
volume bar cannot. **Volume is unsigned**: it tells you how much traded, never
who was the aggressor. A 10M-share day looks identical whether buyers lifted
every offer or sellers hit every bid.

Recovering the true sign needs trade-level data with the prevailing bid/ask —
the Lee-Ready test: a print at the ask is a buy, at the bid a sell. Yahoo's free
feed has no such thing, so the sign is **estimated** from OHLCV bars. Rather
than pick one estimator and present it as truth, the scanner runs three
independent ones and reports how many agree:

| | method | what it sees |
|---|---|---|
| **BVC** | Bulk Volume Classification — Easley, López de Prado & O'Hara (2012). buy share = Φ(ΔP ∕ σ<sub>ΔP</sub>) | a *probabilistic* split, not all-or-nothing. The classifier VPIN is built on |
| **CLV** | Close Location Value (Chaikin): ((C−L)−(H−C))∕(H−L) | where the close sits inside the bar's own range — immune to gaps |
| **Tick** | the classic tick rule, sign(ΔP) × V, zero-ticks carried forward | crude binary baseline, kept as a third opinion |

**Which estimator, on which bars, matters more than the estimator itself.**
Daily bars are signed with **CLV** and intraday bars with **BVC** — getting this
backwards is the easiest way to build a misleading tool. A daily close-to-close
move *is* the price trend, so a BVC-signed daily CVD can barely diverge from
price and the divergence test goes blind; where the close sits inside the day's
range is genuinely new information. At 5-minute resolution the opposite holds:
bar-to-bar change really is an aggression proxy, which is where BVC shines.

### What the dashboard shows

- **Flow score, −100…+100** — one composite reading, with a hoverable bar per
  component so it is never a black box. The *within-bar* family (net signed
  volume, price↔CVD divergence, CMF, Twiggs MF) carries most of the weight on
  purpose: the close-to-close family (OBV, MFI, Force Index) is largely a
  volume-weighted echo of the price trend, and weighting it equally would make
  every uptrend read as "accumulation" — i.e. say nothing a price chart doesn't.
- **Price vs cumulative volume delta (CVD)** — the most useful read here. Price
  and CVD normally travel together; when they part company one of them is
  lying. Price making higher highs while CVD makes lower highs means the rally
  is being **sold into** — escaping under cover of a rising tape. The mirror
  case means dips are being **absorbed**.
- **VPIN** (Easley, López de Prado & O'Hara) — order-flow toxicity. Buckets the
  tape by *volume* rather than by the clock and measures how one-sided each
  bucket was. High readings mean someone is working a large one-way order;
  it spiked ahead of the 2010 Flash Crash, which is what made it famous.
- **Wyckoff effort vs result** — heavy volume that produces almost no price
  movement means someone large is absorbing everything thrown at them. At the
  lows that is accumulation, at the highs distribution — and it shows up
  *before* the move does.
- **Signed volume profile** (`vol@price`) — a poor-man's footprint: bar length
  is volume traded at that price, colour is who won there. Heavy red shelves
  are supply, and price tends to stall into them. Plus POC / value area / VWAP.
- **Classical confirmation set** — OBV, A/D line, CMF, **Twiggs Money Flow**
  (CMF rebuilt on *true* range so overnight gaps don't corrupt it — it matters
  for exactly the gappy names this repo screens), MFI, Force Index, VPT, Ease
  of Movement, O'Neil's up/down volume ratio, IBD-style accumulation and
  distribution days, and Amihud price impact.
- **Market and sector aggregates** — net signed dollar volume rolled up by GICS
  sector makes *rotation* directly visible: money leaving one sector and
  arriving in another on the same day, which no single-name view can show.
- **ETF panel** — for a fund, flow is the most direct read there is of whether
  money is entering or leaving a theme. Broad / sector / thematic / macro.
- **Live tab** — recomputes intraday flow in the browser from fresh 5-minute
  candles via `/api/chart`, so you are not stuck with the snapshot's age.

### On your own charts

`indicators/flow_delta.pine` is the same method as a TradingView indicator
(Pine v5): signed volume, CVD, automatic divergence markers, Wyckoff absorption
markers and alert conditions. Pine Editor → paste → Add to chart.

### Running it

```sh
python tools/test_flow_lib.py          # 104 self-tests, no network needed
python tools/scan_flow.py --limit 60   # quick local validation
python tools/scan_flow.py              # full universe → flow_results.json
python tools/scan_flow.py --etfs-only  # just the ETF panel
```

### Flow-specific caveats

- **These are estimates, not exchange order flow.** Every number on the page is
  derived from free OHLCV bars. Treat the three-method agreement badge as the
  confidence measure, and treat a 1-of-3 reading as noise.
- The daily read scores **today's bar while it is still forming** when the
  scheduled scan runs mid-session. The 19:00 UTC run is late enough to be
  meaningful; a post-close run would be cleaner.
- Yahoo's intraday history is capped (1m ≈ 7 days, 5m ≈ 60 days), so the
  intraday panel covers 5 sessions.
- Signed volume is meaningless on illiquid names, so the scan floors at $5
  price and $3M average daily dollar volume.

### Ideas not wired up yet

The methods above are everything that can be derived honestly from free bar
data. These need a data source this repo doesn't have yet, roughly in order of
value per unit of effort:

| idea | what it adds | source |
|---|---|---|
| **True tick-level order flow** | the real thing — every print classified against the actual bid/ask, and a genuine footprint chart. Replaces every estimate on the page | Polygon (the `polygonQuotes()` stub already exists), Databento, IEX DEEP |
| **FINRA short-sale volume** | daily off-exchange (dark-pool) short volume ratio per symbol — the most-watched free proxy for institutional activity | `cdn.finra.org/equity/regsho/daily/` (free CSV) |
| **FINRA ATS / dark-pool volume** | weekly per-symbol volume actually crossed in dark pools | FINRA OTC Transparency (free, weekly, delayed) |
| **ETF creation / redemption** | for a fund this is *literally* money in or out: Δ shares outstanding × NAV. No estimation involved | issuer daily holdings files |
| **Order-book imbalance / iceberg detection** | pressure sitting on the book rather than pressure already executed | Level 2 feed |
| **NYSE up/down volume, TICK, TRIN** | the purest market-wide injecting-vs-escaping measure, and a much better market breadth gauge than the aggregate here | index data vendor |
| **Options dealer positioning** | gamma/charm exposure often explains why flow stalls at a price. The options scanner here already pulls the chain | extend `tools/scan_options.py` |
| **Form 4 / 13F / 13D-G** | slow but unambiguous: who actually bought, filed and signed | SEC EDGAR (free) |
| **Short interest & borrow fee** | separates real selling from short selling — a distribution reading driven by shorts behaves very differently | exchange bi-monthly, broker API for borrow |


## Data caveats (free Yahoo feed)

- `marketState=PRE` populates `preMarketPrice` / `preMarketChangePercent`. Outside
  the pre-market window those are null and the app uses the regular-session change.
- **Volume / rel-vol reflect the regular session**, not pre-market-only share
  volume — Yahoo's quote endpoint doesn't expose per-session pre-market volume.
  Polygon's snapshot endpoint does.
- A few individual names occasionally return a **wrong price or market cap** from
  the unofficial endpoint (the % change stays correct). The cap-bucket filter
  trusts Yahoo's reported cap; the paid feed is more reliable.
- Yahoo's endpoints are unofficial and can change without notice.

## Run / deploy

This machine has no Node, so the function can't run locally — but everything is
testable with Python:

```sh
python tools/probe.py          # live scan against Yahoo + refresh the snapshot
python tools/probe.py --no-write
```

Static preview of the UI (uses the snapshot, no function):

```sh
python -m http.server 8754     # then open /premarket-scanner/index.html
```

Deploy: the GitHub repo is connected to **Vercel**, which auto-deploys the
static pages and the `api/` functions on every push. The scheduled scans
(`.github/workflows/scan.yml`) commit fresh snapshots, and the Options / Levels
/ Money Flow dashboards read those from GitHub raw so data stays current between
deploys.

## Refreshing the universe

```sh
python tools/gen_universe.py   # (the script used to build universe.json)
```

Pulls the S&P 500 / 400 / 600 constituent tables (GICS sectors) from Wikipedia
and merges them. Re-run periodically to pick up index add/drops. To widen
further, append entries to `universe.json` with `symbol`, `yahoo` (dots→dashes,
e.g. `BRK.B`→`BRK-B`), `name`, and `sector`. The function fetches quotes in
parallel batches, so ~1,500 names stay well within the function timeout.
