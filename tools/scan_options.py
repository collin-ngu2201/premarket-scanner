#!/usr/bin/env python3
"""Options IV-rank / premium screener — the backend for options.html.

Pipeline (designed to run on a schedule, e.g. GitHub Actions hourly):
  1. Bulk Yahoo quotes for the universe -> price, earnings, market cap.
  2. Keep price >= PRICE_FLOOR (cheap pre-filter; UI floor can only go higher).
  3. Daily 1y history per name -> EMA20/50/200, RSI14, realized-vol series.
  4. Keep only daily UPTREND: close > EMA20 > EMA50 > EMA200, EMA20 sloping up,
     not "broken" (close back under EMA20). Downtrends/broken EMAs are dropped.
  5. For survivors, pull the options chain, pick the ~30-45 DTE expiration and the
     put nearest 0.30 delta (Black-Scholes delta from Yahoo's per-contract IV),
     and compute the cash-secured-put credit + annualized return on cash.
  6. IV-rank: until a real IV history accrues, use an "IV vs 1y realized-vol
     percentile" proxy. The run also appends today's ATM IV to iv_history.json so
     a true 52-week IV rank takes over once ~40+ days exist.

Writes scan_results.json (consumed by the dashboard) and iv_history.json.
RSI ceiling, IV-rank threshold and price floor are NOT hard-applied here (they're
selectable in the UI); only the structural uptrend filter is enforced.

    python tools/scan_options.py --limit 60      # fast local validation
    python tools/scan_options.py                  # full universe
"""
import argparse, http.cookiejar, json, math, os, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
PRICE_FLOOR = 10.0          # base pre-filter; dashboard floor can only go higher
RISK_FREE = 0.045
TARGET_DTE = (28, 45)       # preferred CSP window
TARGET_DELTA = 0.30
HV_WINDOW = 20
MIN_OI = 50                 # option liquidity gate (open interest)
MAX_SPREAD = 0.60           # max (ask-bid)/mid — rejects untradable wide markets
MIN_BID = 0.05
DELTA_BAND = (0.15, 0.45)   # only OTM puts in this delta range qualify as a CSP

# --------------------------------------------------------------------------- auth
def yahoo_auth():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    def g(u):
        return op.open(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=30)
    for u in ("https://fc.yahoo.com", "https://finance.yahoo.com/"):
        try: g(u)
        except Exception: pass
    crumb = g("https://query1.finance.yahoo.com/v1/test/getcrumb").read().decode().strip()
    if not crumb or "<" in crumb:
        raise RuntimeError("crumb fetch failed")
    cookie = "; ".join(f"{c.name}={c.value}" for c in cj)
    return cookie, crumb

def http_json(url, cookie=None, tries=3):
    for i in range(tries):
        try:
            h = {"User-Agent": UA}
            if cookie: h["Cookie"] = cookie
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=h), timeout=30).read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and i < tries - 1:
                time.sleep(1.5 * (i + 1)); continue
            raise
        except Exception:
            if i < tries - 1: time.sleep(1.0); continue
            raise

# --------------------------------------------------------------------------- math
def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bs_put_delta(S, K, T, sigma, r=RISK_FREE):
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return ncdf(d1) - 1.0     # put delta in [-1, 0]

def ema_last_and_series(closes, n):
    if len(closes) < n: return None, []
    k = 2.0 / (n + 1)
    ema = sum(closes[:n]) / n
    series = [None] * (n - 1) + [ema]
    for c in closes[n:]:
        ema = c * k + ema * (1 - k)
        series.append(ema)
    return ema, series

def rsi_last(closes, period=14):
    if len(closes) < period + 1: return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    if al == 0: return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)

def hv_series(closes, window=HV_WINDOW):
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0]
    out = []
    for i in range(window, len(rets) + 1):
        w = rets[i - window:i]
        m = sum(w) / window
        var = sum((x - m) ** 2 for x in w) / (window - 1)
        out.append(math.sqrt(var) * math.sqrt(252))
    return out

# --------------------------------------------------------------------------- per-symbol
def daily_metrics(yh):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(yh)}"
           f"?range=1y&interval=1d")
    d = http_json(url)
    res = (d.get("chart", {}).get("result") or [None])[0]
    if not res: return None
    closes = [c for c in (res.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c is not None]
    if len(closes) < 60: return None
    last = closes[-1]
    e20, s20 = ema_last_and_series(closes, 20)
    e50, _ = ema_last_and_series(closes, 50)
    e200, _ = ema_last_and_series(closes, 200)
    rsi = rsi_last(closes, 14)
    hv = hv_series(closes, HV_WINDOW)
    if None in (e20, e50, e200) or rsi is None or not hv:
        return None
    slope_up = len(s20) >= 6 and s20[-1] is not None and s20[-6] is not None and s20[-1] > s20[-6]
    broken = last < e20
    trend_up = (last > e20 > e50 > e200) and slope_up and not broken
    return {"close": round(last, 2), "ema20": round(e20, 2), "ema50": round(e50, 2),
            "ema200": round(e200, 2), "rsi14": round(rsi, 1), "hv20": round(hv[-1], 4),
            "hvSeries": hv, "trendUp": trend_up, "broken": broken}

def option_metrics(yh, spot, cookie):
    base = f"https://query1.finance.yahoo.com/v7/finance/options/{urllib.parse.quote(yh)}"
    d = http_json(f"{base}?crumb={urllib.parse.quote(cookie[1])}", cookie=cookie[0])
    res = (d.get("optionChain", {}).get("result") or [None])[0]
    if not res: return None
    exps = res.get("expirationDates") or []
    if not exps: return None
    now = time.time()
    def dte(e): return (e - now) / 86400.0
    in_win = [e for e in exps if TARGET_DTE[0] <= dte(e) <= TARGET_DTE[1]]
    target = min(in_win, key=lambda e: abs(dte(e) - 35)) if in_win else \
        min([e for e in exps if dte(e) > 5], key=lambda e: abs(dte(e) - 35), default=None)
    if not target: return None
    d2 = http_json(f"{base}?date={int(target)}&crumb={urllib.parse.quote(cookie[1])}", cookie=cookie[0])
    res2 = (d2.get("optionChain", {}).get("result") or [None])[0]
    opt = (res2.get("options") or [None])[0]
    if not opt: return None
    puts = []
    for p in opt.get("puts", []):
        bid, ask = p.get("bid"), p.get("ask")
        strike, iv, oi = p.get("strike"), p.get("impliedVolatility"), p.get("openInterest") or 0
        if not (bid and strike and iv): continue
        if bid < MIN_BID or oi < MIN_OI: continue        # liquidity gate
        mid = (bid + (ask or bid)) / 2
        if mid > 0 and ((ask or bid) - bid) / mid > MAX_SPREAD: continue   # spread gate
        puts.append(p)
    if not puts: return None
    T = max(dte(target), 1) / 365.0
    # OTM puts whose BS delta sits in the band; pick the one nearest TARGET_DELTA
    best, bestd = None, 1e9
    for p in puts:
        if p["strike"] >= spot: continue                 # OTM puts only
        dlt = bs_put_delta(spot, p["strike"], T, p["impliedVolatility"])
        if dlt is None or not (DELTA_BAND[0] <= abs(dlt) <= DELTA_BAND[1]): continue
        diff = abs(abs(dlt) - TARGET_DELTA)
        if diff < bestd: best, bestd = (p, dlt), diff
    if not best: return None
    p, dlt = best
    strike = p["strike"]; bid = p["bid"]; ask = p.get("ask") or bid
    credit = round((bid + ask) / 2, 2)
    days = max(dte(target), 1)
    annpct = round((credit / strike) * (365.0 / days) * 100, 1)
    # ATM IV (strike nearest spot) for the IV-rank measure
    atm = min(puts, key=lambda x: abs(x["strike"] - spot))
    atm_iv = atm.get("impliedVolatility")
    return {"putStrike": strike, "putDTE": round(days), "putDelta": round(dlt, 2),
            "putCredit": credit, "annPct": annpct, "atmIV": round(atm_iv, 4),
            "expiry": datetime.utcfromtimestamp(target).strftime("%Y-%m-%d")}

# --------------------------------------------------------------------------- bulk quotes
QFIELDS = ("symbol,shortName,regularMarketPrice,marketCap,regularMarketVolume,"
           "earningsTimestamp,earningsTimestampStart")
def bulk_quotes(yahoos, cookie):
    out = {}
    for i in range(0, len(yahoos), 100):
        grp = yahoos[i:i + 100]
        url = (f"https://query1.finance.yahoo.com/v7/finance/quote?symbols="
               f"{urllib.parse.quote(','.join(grp))}&fields={QFIELDS}"
               f"&crumb={urllib.parse.quote(cookie[1])}")
        d = http_json(url, cookie=cookie[0])
        for q in d.get("quoteResponse", {}).get("result", []):
            out[q.get("symbol")] = q
        time.sleep(0.1)
    return out

# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="cap universe (for testing)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    univ = json.load(open(os.path.join(ROOT, "universe.json"), encoding="utf-8"))
    names = univ["constituents"]
    if args.limit: names = names[:args.limit]
    meta = {n["yahoo"]: n for n in names}
    yahoos = [n["yahoo"] for n in names]
    print(f"universe: {len(yahoos)} names | workers={args.workers}", flush=True)

    cookie = yahoo_auth()
    print("auth ok", flush=True)

    quotes = bulk_quotes(yahoos, cookie)
    now = time.time()
    priced = []
    for yh in yahoos:
        q = quotes.get(yh) or {}
        px = q.get("regularMarketPrice")
        if isinstance(px, (int, float)) and px >= PRICE_FLOOR:
            priced.append(yh)
    print(f"price >= ${PRICE_FLOOR}: {len(priced)}", flush=True)

    # daily indicators -> uptrend filter
    daily = {}
    def _daily(yh):
        try: return yh, daily_metrics(yh)
        except Exception: return yh, None
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for yh, m in ex.map(_daily, priced):
            if m: daily[yh] = m
    uptrend = [yh for yh in priced if daily.get(yh, {}).get("trendUp")]
    print(f"daily uptrend (EMA stack): {len(uptrend)}", flush=True)

    # options for uptrend names
    opts = {}
    def _opt(yh):
        try: return yh, option_metrics(yh, daily[yh]["close"], cookie)
        except Exception: return yh, None
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for yh, m in ex.map(_opt, uptrend):
            if m: opts[yh] = m
    print(f"with tradable ~30-45 DTE put: {len(opts)}", flush=True)

    # IV history accrual + iv-rank (real if enough history, else hv-percentile proxy)
    hist_path = os.path.join(ROOT, "iv_history.json")
    hist = json.load(open(hist_path)) if os.path.exists(hist_path) else {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    results = []
    for yh in opts:
        d, o = daily[yh], opts[yh]
        q = quotes.get(yh, {})
        iv = o["atmIV"]
        # accrue
        h = hist.setdefault(yh, [])
        if not h or h[-1][0] != today: h.append([today, round(iv, 4)])
        if len(h) > 300: del h[:-300]    # keep ~52 weeks of trading days
        ivs = [v for _, v in h]
        if len(ivs) >= 40:
            lo, hi = min(ivs), max(ivs)
            ivrank = round(100 * (iv - lo) / (hi - lo), 0) if hi > lo else 50.0
            src = "real"
        else:  # proxy: IV percentile within 1y realized-vol distribution
            hv = d["hvSeries"]
            ivrank = round(100 * sum(1 for x in hv if x < iv) / len(hv), 0)
            src = "hv-proxy"
        # earnings
        ets = q.get("earningsTimestamp") or q.get("earningsTimestampStart")
        days_e = round((ets - now) / 86400.0) if isinstance(ets, (int, float)) else None
        results.append({
            "symbol": meta[yh]["symbol"], "yahoo": yh, "name": meta[yh]["name"],
            "sector": meta[yh]["sector"], "price": d["close"],
            "ivRank": ivrank, "ivRankSrc": src, "atmIV": round(iv * 100, 1), "hv20": round(d["hv20"] * 100, 1),
            "annPct": o["annPct"], "putCredit": o["putCredit"], "putStrike": o["putStrike"],
            "putDTE": o["putDTE"], "putDelta": o["putDelta"], "expiry": o["expiry"],
            "rsi14": d["rsi14"], "ema20": d["ema20"], "ema50": d["ema50"], "ema200": d["ema200"],
            "earnDays": days_e, "earnSoon": (days_e is not None and 0 <= days_e <= 30),
            "earnDate": (datetime.utcfromtimestamp(ets).strftime("%Y-%m-%d") if isinstance(ets, (int, float)) else None),
            "marketCap": q.get("marketCap"),
        })

    results.sort(key=lambda r: r["annPct"], reverse=True)
    json.dump(hist, open(hist_path, "w"))
    out = {
        "asOf": datetime.now(timezone.utc).isoformat(),
        "universe": univ["source"], "scanned": len(yahoos),
        "passedPrice": len(priced), "passedTrend": len(uptrend),
        "params": {"priceFloor": PRICE_FLOOR, "targetDTE": TARGET_DTE, "targetDelta": TARGET_DELTA},
        "count": len(results), "results": results,
    }
    json.dump(out, open(os.path.join(ROOT, "scan_results.json"), "w"), indent=0)
    print(f"\nRESULTS: {len(results)} candidates -> scan_results.json", flush=True)
    for r in results[:12]:
        e = f"earnings {r['earnDays']}d" if r["earnSoon"] else ""
        print(f"  {r['symbol']:<6} ann={r['annPct']:>6}%  IVrank={r['ivRank']:>3}({r['ivRankSrc']})  "
              f"RSI={r['rsi14']:>4}  ${r['price']:<7} put {r['putStrike']}/{r['putDTE']}d d{r['putDelta']}  {e}", flush=True)

if __name__ == "__main__":
    main()
