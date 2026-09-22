#!/usr/bin/env python3
"""Money-flow / order-flow scanner -- backend for flow.html.

Answers the question a volume bar cannot: for each name, is money being
INJECTED (accumulation) or is it ESCAPING (distribution)? See tools/flow_lib.py
for the estimators; this file is just the data plumbing around them.

Two stages, so the whole S&P 1500 stays inside one GitHub Action run:

  Stage 1  one daily-bar request per name (1y of 1d candles) -> analyze_daily.
           Runs on EVERYTHING, because the market-wide and per-sector net-flow
           aggregates are only meaningful if nothing was filtered out first.
  Stage 2  one intraday request (5d of 5m candles, incl. pre/post) for the
           most extreme names plus every ETF -> analyze_intraday. This is the
           expensive half, so it is reserved for names worth drilling into.

Writes flow_results.json. Like the other scanners here it uses only Yahoo's
keyless v8 chart endpoint (no crumb/auth) and the Python standard library.

    python tools/scan_flow.py --limit 60      # quick local validation
    python tools/scan_flow.py                  # full universe
    python tools/scan_flow.py --etfs-only      # just the ETF panel
"""
import argparse, collections, json, os, sys, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flow_lib as F

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

PRICE_FLOOR = 5.0            # skip sub-$5 names: the flow read gets noisy
MIN_DOLLAR_VOL = 3e6         # skip illiquid names: signed volume is meaningless
KEEP_SCORE = 18              # |score| at or above this is worth showing
MAX_KEEP = 420               # cap the snapshot size
INTRADAY_TOP = 200           # stage-2 budget (plus all ETFs)
SPARK_POINTS = 60            # daily CVD sparkline resolution
INTRADAY_POINTS = 90         # intraday CVD line resolution

# ETFs get their own panel: for a fund, flow is the most direct read there is
# of whether the market is putting money into a theme or pulling it out.
ETFS = [
    ("SPY", "S&P 500", "Broad"), ("QQQ", "Nasdaq 100", "Broad"),
    ("IWM", "Russell 2000", "Broad"), ("DIA", "Dow 30", "Broad"),
    ("MDY", "S&P Midcap 400", "Broad"), ("RSP", "S&P 500 Equal Weight", "Broad"),
    ("XLK", "Technology", "Sector"), ("XLF", "Financials", "Sector"),
    ("XLE", "Energy", "Sector"), ("XLV", "Health Care", "Sector"),
    ("XLI", "Industrials", "Sector"), ("XLY", "Cons. Discretionary", "Sector"),
    ("XLP", "Cons. Staples", "Sector"), ("XLU", "Utilities", "Sector"),
    ("XLB", "Materials", "Sector"), ("XLRE", "Real Estate", "Sector"),
    ("XLC", "Communication Svcs", "Sector"),
    ("SMH", "Semiconductors", "Theme"), ("XBI", "Biotech", "Theme"),
    ("KRE", "Regional Banks", "Theme"), ("ITB", "Homebuilders", "Theme"),
    ("JETS", "Airlines", "Theme"), ("ARKK", "Disruptive Innov.", "Theme"),
    ("TLT", "20y Treasuries", "Macro"), ("IEF", "7-10y Treasuries", "Macro"),
    ("HYG", "High Yield Credit", "Macro"), ("LQD", "IG Credit", "Macro"),
    ("GLD", "Gold", "Macro"), ("SLV", "Silver", "Macro"),
    ("USO", "Crude Oil", "Macro"), ("UUP", "US Dollar", "Macro"),
    ("EEM", "Emerging Mkts", "Macro"), ("EFA", "Developed ex-US", "Macro"),
    ("VNQ", "REITs", "Macro"),
]


def http_json(url, tries=3):
    for i in range(tries):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}),
                timeout=30).read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and i < tries - 1:
                time.sleep(1.2 * (i + 1)); continue
            raise
        except Exception:
            if i < tries - 1:
                time.sleep(0.8); continue
            raise


def candles(yh, rng, interval, prepost=False):
    """Fetch OHLCV from Yahoo's keyless chart endpoint.

    Bars with any null field are dropped wholesale rather than interpolated --
    a synthesised bar would feed a fabricated buy/sell split straight into the
    score. Zero-volume bars go too (holiday stubs and thin pre-market prints
    carry no flow information but do drag the profile around).
    """
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(yh)}"
           f"?range={rng}&interval={interval}"
           f"&includePrePost={'true' if prepost else 'false'}")
    d = http_json(url)
    res = (d.get("chart", {}).get("result") or [None])[0]
    if not res:
        return None
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    ts = res.get("timestamp") or []
    blank = [None] * len(ts)
    op, hi, lo, cl, vo = (q.get(k) or blank for k in ("open", "high", "low", "close", "volume"))
    T, O, H, L, C, V = [], [], [], [], [], []
    for i in range(len(ts)):
        o, h, l, c, v = op[i], hi[i], lo[i], cl[i], vo[i]
        if None in (o, h, l, c) or not v:
            continue
        T.append(ts[i]); O.append(o); H.append(h); L.append(l); C.append(c); V.append(float(v))
    meta = res.get("meta") or {}
    return {"t": T, "o": O, "h": H, "l": L, "c": C, "v": V,
            "gmtoffset": meta.get("gmtoffset") or 0,
            "regStart": (meta.get("currentTradingPeriod") or {}).get("regular", {}).get("start"),
            "regEnd": (meta.get("currentTradingPeriod") or {}).get("regular", {}).get("end")}


def drop_forming_bar(k, now=None):
    """Remove today's daily bar while the session is still open.

    Yahoo returns a bar for the current day from the opening bell, carrying
    only the volume traded so far. Scoring it makes the whole read depend on
    what time the job happened to fire -- and the scheduled runs fire hours
    late and at a different point in the session every day.

    An audit over 60 names found the partial bar moved the composite score by
    a median of only ~2 points, but it flipped the LABEL on 12% of names and
    changed the accumulation/distribution day count on 70% of them. That last
    one is structural, not noise: acc/dist days compare today's volume against
    yesterday's FULL volume, so a bar that is 15% formed can essentially never
    register, and the count silently under-reports.

    So the daily read uses completed sessions only. Today is not lost -- it is
    exactly what the intraday panel is for, and that panel reads today's
    5-minute bars directly.

    Returns (candles, dropped_bar_or_None).
    """
    if not k["t"]:
        return k, None
    off = k.get("gmtoffset") or 0
    now = time.time() if now is None else now      # injectable for tests
    if int((k["t"][-1] + off) // 86400) != int((now + off) // 86400):
        return k, None                       # last bar is an earlier session
    reg_end = k.get("regEnd")
    if reg_end and now >= reg_end:
        return k, None                       # the close has passed: bar is final
    if not reg_end and (now + off) % 86400 >= 16 * 3600:
        return k, None                       # no meta: fall back to the local clock
    last = {key: k[key][-1] for key in ("t", "o", "h", "l", "c", "v")}
    for key in ("t", "o", "h", "l", "c", "v"):
        k[key] = k[key][:-1]
    return k, last


def bar_date(ts, gmtoffset):
    return datetime.fromtimestamp(ts + (gmtoffset or 0), timezone.utc).strftime("%Y-%m-%d")


def downsample(series, n):
    """Evenly thin a series to at most n points, always keeping the last one."""
    if not series or len(series) <= n:
        return series
    step = (len(series) - 1) / (n - 1)
    out = [series[int(round(i * step))] for i in range(n - 1)]
    out.append(series[-1])
    return out


# ---------------------------------------------------------------------------
# stage 1 -- daily flow over the whole universe
# ---------------------------------------------------------------------------

def daily_row(entry):
    yh = entry["yahoo"]
    try:
        k = candles(yh, "1y", "1d")
    except Exception:
        return None
    if not k or len(k["c"]) < 60:
        return None
    # Keep today's live price for DISPLAY before trimming: the flow math wants
    # completed sessions, but a card showing yesterday's close next to a live
    # quote would just look broken.
    live_price, live_prev = k["c"][-1], (k["c"][-2] if len(k["c"]) > 1 else None)
    k, forming = drop_forming_bar(k)
    if not k["c"] or len(k["c"]) < 60:
        return None
    if live_price < PRICE_FLOOR:
        return None
    r = F.analyze_daily(k["h"], k["l"], k["c"], k["v"])
    if not r:
        return None
    if (r.get("dollarVol") or 0) < MIN_DOLLAR_VOL:
        return None
    r["yahoo"] = yh
    r["symbol"] = entry["symbol"]
    r["name"] = entry["name"]
    r["sector"] = entry["sector"]
    r["kind"] = entry.get("kind", "stock")
    r["etfGroup"] = entry.get("etfGroup")
    r["price"] = round(live_price, 2)
    r["chg1d"] = round(100 * (live_price / live_prev - 1), 2) if live_prev else None
    r["chg20d"] = round(100 * (live_price / k["c"][-20] - 1), 2) if len(k["c"]) > 20 else None
    r["flowThrough"] = bar_date(k["t"][-1], k["gmtoffset"])
    r["formingDropped"] = forming is not None
    r["avgVol"] = round(F.mean(k["v"][-20:]) or 0)
    r["cvd"] = downsample(r["cvd"], SPARK_POINTS)
    r["closes"] = downsample(r["closes"], SPARK_POINTS)
    return r


# ---------------------------------------------------------------------------
# stage 2 -- intraday order flow for the names that matter
# ---------------------------------------------------------------------------

def intraday_row(row):
    try:
        k = candles(row["yahoo"], "5d", "5m", prepost=True)
    except Exception:
        return None
    if not k or len(k["c"]) < 40:
        return None
    r = F.analyze_intraday(k["t"], k["h"], k["l"], k["c"], k["v"], adv=row.get("avgVol"))
    if not r:
        return None
    r["cvd"] = downsample(r["cvd"], INTRADAY_POINTS)
    r["closes"] = downsample(r["closes"], INTRADAY_POINTS)
    # Per-point timestamps are the single largest field in the snapshot and the
    # chart only needs to know where one session ends and the next begins, so
    # store the day-boundary indices instead of 90 epoch seconds per name.
    ts = downsample(r.pop("ts"), INTRADAY_POINTS)
    off = k["gmtoffset"] or 0
    days = [(t + off) // 86400 for t in ts]
    r["dayBreaks"] = [i for i in range(1, len(days)) if days[i] != days[i - 1]]
    r["lastTs"] = ts[-1] if ts else None
    return r


# ---------------------------------------------------------------------------
# aggregates -- the market-wide "risk on / risk off" read
# ---------------------------------------------------------------------------

def aggregate(rows):
    """Roll the per-name flow up into a market and per-sector picture.

    Net signed dollar volume by sector is the part worth watching: it makes
    rotation visible directly -- money leaving one sector and arriving in
    another on the same day -- which no single-name view can show.
    """
    stocks = [r for r in rows if r["kind"] == "stock"]
    if not stocks:
        return {}
    tot_usd = sum(r["netDeltaUsd"] or 0 for r in stocks)
    gross = sum(abs(r["netDeltaUsd"] or 0) for r in stocks) or 1
    acc = sum(1 for r in stocks if r["score"] >= 15)
    dis = sum(1 for r in stocks if r["score"] <= -15)
    sectors = {}
    for r in stocks:
        s = sectors.setdefault(r["sector"], {"sector": r["sector"], "n": 0, "usd": 0.0,
                                             "score": 0.0, "acc": 0, "dis": 0})
        s["n"] += 1
        s["usd"] += r["netDeltaUsd"] or 0
        s["score"] += r["score"]
        s["acc"] += 1 if r["score"] >= 15 else 0
        s["dis"] += 1 if r["score"] <= -15 else 0
    for s in sectors.values():
        s["score"] = round(s["score"] / s["n"], 1)
        s["usd"] = round(s["usd"])
    return {
        "netUsd": round(tot_usd),
        "breadth": round(100.0 * tot_usd / gross, 1),      # -100..+100
        "names": len(stocks), "accNames": acc, "disNames": dis,
        "pctAcc": round(100.0 * acc / len(stocks), 1),
        "pctDis": round(100.0 * dis / len(stocks), 1),
        "avgScore": round(sum(r["score"] for r in stocks) / len(stocks), 1),
        "sectors": sorted(sectors.values(), key=lambda s: -s["usd"]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="cap the stock universe")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--intraday", type=int, default=INTRADAY_TOP)
    ap.add_argument("--etfs-only", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--out", default="flow_results.json")
    args = ap.parse_args()

    univ = json.load(open(os.path.join(ROOT, "universe.json"), encoding="utf-8"))
    stocks = univ["constituents"][:args.limit] if args.limit else univ["constituents"]
    if args.etfs_only:
        stocks = []
    targets = [{"symbol": s["symbol"], "yahoo": s["yahoo"], "name": s["name"],
                "sector": s["sector"], "kind": "stock"} for s in stocks]
    targets += [{"symbol": t, "yahoo": t, "name": n, "sector": "ETF",
                 "kind": "etf", "etfGroup": g} for t, n, g in ETFS]

    print(f"stage 1: {len(targets)} symbols (daily flow) | workers={args.workers}",
          flush=True)
    t0 = time.time()
    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, r in enumerate(ex.map(daily_row, targets)):
            if r:
                rows.append(r)
            if (i + 1) % 250 == 0:
                print(f"  ...{i + 1}/{len(targets)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"stage 1 done: {len(rows)} analysed in {time.time() - t0:.0f}s", flush=True)

    agg = aggregate(rows)

    # Keep the names with something to say: a strong score, a price/flow
    # divergence, or Wyckoff absorption. ETFs are always kept -- the panel is
    # a fixed set and a neutral reading on SPY is itself information.
    def interesting(r):
        return (r["kind"] == "etf" or abs(r["score"]) >= KEEP_SCORE
                or r["divergence"] != 0 or r["absorption"] >= 2)

    kept = [r for r in rows if interesting(r)]
    kept.sort(key=lambda r: (r["kind"] != "etf", -abs(r["score"])))
    kept = [r for r in kept if r["kind"] == "etf"][:len(ETFS)] + \
           [r for r in kept if r["kind"] != "etf"][:MAX_KEEP]

    # stage 2: every ETF, plus the most extreme stocks
    picks = [r for r in kept if r["kind"] == "etf"] + \
            sorted([r for r in kept if r["kind"] == "stock"],
                   key=lambda r: -abs(r["score"]))[:args.intraday]
    print(f"stage 2: {len(picks)} symbols (intraday order flow)", flush=True)
    t1 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for row, intra in zip(picks, ex.map(intraday_row, picks)):
            if intra:
                row["intraday"] = intra
    got = sum(1 for r in kept if r.get("intraday"))
    print(f"stage 2 done: {got} with intraday flow in {time.time() - t1:.0f}s", flush=True)

    # The scan is deliberate about which sessions it scored, so say so in the
    # snapshot rather than leaving the dashboard to imply "as of now".
    through = collections.Counter(r.get("flowThrough") for r in rows if r.get("flowThrough"))
    flow_through = through.most_common(1)[0][0] if through else None
    dropped = sum(1 for r in rows if r.get("formingDropped"))

    out = {
        "asOf": datetime.now(timezone.utc).isoformat(),
        "flowThrough": flow_through,
        "formingBarDropped": dropped,
        "universe": univ["source"],
        "scanned": len(targets), "analysed": len(rows), "count": len(kept),
        "params": {"priceFloor": PRICE_FLOOR, "minDollarVol": MIN_DOLLAR_VOL,
                   "keepScore": KEEP_SCORE, "dailyMethod": F.DAILY_METHOD,
                   "intradayMethod": F.INTRADAY_METHOD},
        "weights": [{"key": k, "w": w, "label": lbl} for k, w, lbl in F.SCORE_WEIGHTS],
        "market": agg,
        "results": kept,
    }
    if not args.no_write:
        path = os.path.join(ROOT, args.out)
        json.dump(out, open(path, "w"), indent=0)
        size = os.path.getsize(path) / 1e6
        print(f"\nwrote {args.out} ({size:.2f} MB)", flush=True)

    print(f"\nflow scored through {flow_through} "
          f"({dropped}/{len(rows)} had today's forming bar dropped)", flush=True)
    print(f"MARKET: net signed flow ${agg.get('netUsd', 0) / 1e9:+.2f}B  "
          f"breadth {agg.get('breadth', 0):+.1f}  "
          f"{agg.get('pctAcc', 0)}% accumulating / {agg.get('pctDis', 0)}% distributing",
          flush=True)
    for s in (agg.get("sectors") or [])[:4]:
        print(f"   + {s['sector']:<24} ${s['usd'] / 1e9:+.2f}B  avg score {s['score']:+.1f}",
              flush=True)
    for s in (agg.get("sectors") or [])[-3:]:
        print(f"   - {s['sector']:<24} ${s['usd'] / 1e9:+.2f}B  avg score {s['score']:+.1f}",
              flush=True)

    print("\nTOP ACCUMULATION", flush=True)
    for r in sorted(kept, key=lambda r: -r["score"])[:8]:
        print(f"  {r['symbol']:<6} {r['score']:+6.1f} {r['label']:<20} "
              f"net {r['netDeltaPct']:+5.1f}% of vol  CMF {r['cmf']}  U/D {r['ud']}",
              flush=True)
    print("TOP DISTRIBUTION", flush=True)
    for r in sorted(kept, key=lambda r: r["score"])[:8]:
        print(f"  {r['symbol']:<6} {r['score']:+6.1f} {r['label']:<20} "
              f"net {r['netDeltaPct']:+5.1f}% of vol  CMF {r['cmf']}  U/D {r['ud']}",
              flush=True)
    divs = [r for r in kept if r["divergence"] != 0]
    print(f"\nPRICE/FLOW DIVERGENCES: {len(divs)}", flush=True)
    for r in divs[:8]:
        kind = "bearish (rallies sold)" if r["divergence"] < 0 else "bullish (dips bought)"
        print(f"  {r['symbol']:<6} {kind:<24} px20d {r['chg20d']:+.1f}%  score {r['score']:+.1f}",
              flush=True)


if __name__ == "__main__":
    main()
