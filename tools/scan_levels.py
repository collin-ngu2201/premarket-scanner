#!/usr/bin/env python3
"""Key-levels screener — backend for levels.html.

Finds stocks trading near a major technical level, ranked by CONFLUENCE
(how many distinct level-types stack at the current price). Three types:
  1. 200MA   — distance of price to the 200-day SMA
  2. Support — nearest of {clustered swing/pivot lows, the 50-day SMA,
               the recent consolidation low}
  3. Fib     — nearest retracement level of the 52-week range AND the recent
               (~6-month) swing (23.6/38.2/50/61.8/78.6%)

Uses only Yahoo's keyless v8 daily-chart endpoint (no crumb/auth). Writes
levels_results.json. The dashboard re-applies the "near" threshold live, so the
scan stores the raw per-type distances + the level values (for drawing on charts).

    python tools/scan_levels.py --limit 80     # quick local validation
    python tools/scan_levels.py                 # full universe
"""
import argparse, json, math, os, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
PRICE_FLOOR = 10.0
CONF_THR = 0.03            # "near" = within 3% (default; UI can change)
STORE_THR = 0.06           # keep a name if it's within 6% of any level type
FIB_RATIOS = [0.236, 0.382, 0.5, 0.618, 0.786]
PIVOT_W = 7
MAX_KEEP = 600

def http_json(url, tries=3):
    for i in range(tries):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30).read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and i < tries - 1: time.sleep(1.2 * (i + 1)); continue
            raise
        except Exception:
            if i < tries - 1: time.sleep(0.8); continue
            raise

def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None

def pivot_lows(lows, w=PIVOT_W):
    out = []
    for i in range(w, len(lows) - w):
        if lows[i] == min(lows[i - w:i + w + 1]): out.append(lows[i])
    return out

def cluster(prices, tol=0.015):
    if not prices: return []
    prices = sorted(prices); zones = []; grp = [prices[0]]
    for p in prices[1:]:
        if (p - grp[-1]) / grp[-1] > tol: zones.append(sum(grp) / len(grp)); grp = []
        grp.append(p)
    zones.append(sum(grp) / len(grp))
    return zones

def fib_levels(hi, lo):
    if hi <= lo: return []
    return [(round(hi - (hi - lo) * r, 2), r) for r in FIB_RATIOS]

def metrics(yh):
    d = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(yh)}?range=2y&interval=1d")
    res = (d.get("chart", {}).get("result") or [None])[0]
    if not res: return None
    q = res.get("indicators", {}).get("quote", [{}])[0]
    closes = q.get("close") or []; highs = q.get("high") or []; lows = q.get("low") or []
    # drop trailing/interior nulls in parallel
    C, H, L = [], [], []
    for c, h, l in zip(closes, highs, lows):
        if None in (c, h, l): continue
        C.append(c); H.append(h); L.append(l)
    if len(C) < 210: return None
    price = C[-1]
    if price < PRICE_FLOOR: return None
    s50, s200 = sma(C, 50), sma(C, 200)
    if not s200: return None

    d200 = (price - s200) / price

    # --- support candidates: pivot zones + 50MA + recent consolidation low ---
    piv = cluster(pivot_lows(L[-252:]))
    consol = min(L[-30:-2]) if len(L) > 32 else None
    cand = [(z, "swing") for z in piv]
    if s50: cand.append((s50, "50MA"))
    if consol: cand.append((consol, "consol"))
    # nearest support to price
    sup_near = min(cand, key=lambda z: abs(price - z[0])) if cand else None
    dSup = abs(price - sup_near[0]) / price if sup_near else 9.9
    supLevel, supKind = (round(sup_near[0], 2), sup_near[1]) if sup_near else (None, None)
    # support lines to draw (zones within 15% of price)
    supLines = sorted({round(z, 2) for z, _ in cand if abs(price - z) / price <= 0.15})

    # --- fibonacci: 52-week + recent (~126d) swing ---
    hi52, lo52 = max(H[-252:]), min(L[-252:])
    hiR, loR = max(H[-126:]), min(L[-126:])
    fibs = [(lv, r, "52w") for lv, r in fib_levels(hi52, lo52)] + \
           [(lv, r, "rec") for lv, r in fib_levels(hiR, loR)]
    fib_near = min(fibs, key=lambda f: abs(price - f[0])) if fibs else None
    dFib = abs(price - fib_near[0]) / price if fib_near else 9.9
    fibLevel, fibRatio, fibSwing = (fib_near[0], fib_near[1], fib_near[2]) if fib_near else (None, None, None)
    fibLines = sorted({lv for lv, _, _ in fibs if abs(price - lv) / price <= 0.15})

    conf3 = sum([abs(d200) <= CONF_THR, dSup <= CONF_THR, dFib <= CONF_THR])
    near_any = min(abs(d200), dSup, dFib) <= STORE_THR
    if not near_any: return None

    return {
        "yahoo": yh, "price": round(price, 2),
        "sma50": round(s50, 2) if s50 else None, "sma200": round(s200, 2),
        "d200": round(d200, 4), "dSup": round(dSup, 4), "dFib": round(dFib, 4),
        "supLevel": supLevel, "supKind": supKind, "supLines": supLines,
        "fibLevel": fibLevel, "fibRatio": fibRatio, "fibSwing": fibSwing, "fibLines": fibLines,
        "hi52": round(hi52, 2), "lo52": round(lo52, 2),
        "aboveMA": d200 > 0, "conf3": conf3,
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    univ = json.load(open(os.path.join(ROOT, "universe.json"), encoding="utf-8"))
    names = univ["constituents"][:args.limit] if args.limit else univ["constituents"]
    meta = {n["yahoo"]: n for n in names}
    print(f"universe: {len(names)} | workers={args.workers}", flush=True)

    res = []
    def work(n):
        try: return metrics(n["yahoo"])
        except Exception: return None
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for m in ex.map(work, names):
            if m: res.append(m)
    print(f"near a level (<= {int(STORE_THR*100)}%): {len(res)}", flush=True)

    for r in res:
        u = meta[r["yahoo"]]; r["symbol"] = u["symbol"]; r["name"] = u["name"]; r["sector"] = u["sector"]
    # sort: most confluence first, then closest overall
    res.sort(key=lambda r: (-r["conf3"], min(abs(r["d200"]), r["dSup"], r["dFib"])))
    res = res[:MAX_KEEP]

    out = {"asOf": datetime.now(timezone.utc).isoformat(), "universe": univ["source"],
           "scanned": len(names), "params": {"priceFloor": PRICE_FLOOR, "confThr": CONF_THR},
           "count": len(res), "results": res}
    json.dump(out, open(os.path.join(ROOT, "levels_results.json"), "w"), indent=0)
    print(f"\nRESULTS: {len(res)} near-level names -> levels_results.json", flush=True)
    for r in res[:12]:
        tags = []
        if abs(r["d200"]) <= CONF_THR: tags.append(f"200MA {r['d200']*100:+.1f}%")
        if r["dSup"] <= CONF_THR: tags.append(f"sup({r['supKind']}) {r['dSup']*100:.1f}%")
        if r["dFib"] <= CONF_THR: tags.append(f"fib{int(r['fibRatio']*100)}({r['fibSwing']}) {r['dFib']*100:.1f}%")
        print(f"  {r['symbol']:<6} ${r['price']:<8} conf={r['conf3']}  {'  '.join(tags)}", flush=True)

if __name__ == "__main__":
    main()
