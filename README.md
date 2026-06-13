# US Pre-Market Scanner

A standalone web app that scans the S&P 500 for pre-market movers and lets you
filter by volume, market cap, sector, gainers/losers, and deviation from the
main indexes (SPY / QQQ). Built to mirror the deploy pattern of the sibling
`market-dashboard` (Netlify static site + serverless function).

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
universe.json                  S&P 500 constituents + GICS sector (503 names)
sample_quotes.json             snapshot fallback (written by tools/probe.py)
netlify/functions/quotes.mjs   batched quote proxy → /api/quotes
tools/probe.py                 local validator + snapshot generator (no Node needed)
netlify.toml                   Netlify config
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
`POLYGON_KEY` in Netlify env, and call `/api/quotes?provider=polygon`. No
front-end changes needed.

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

Deploy (gets the live `/api/quotes` function): push to a Git repo connected to
Netlify, same as `market-dashboard`.

## Refreshing the universe

```sh
python tools/gen_universe.py   # (the script used to build universe.json)
```

Re-run periodically to pick up S&P 500 add/drops. To widen coverage (Nasdaq-100,
Russell 1000), append entries to `universe.json` with `symbol`, `yahoo` (dots→
dashes, e.g. `BRK.B`→`BRK-B`), `name`, and `sector`.
