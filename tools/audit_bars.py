#!/usr/bin/env python3
"""Bar-quality audit for the money-flow scan.

The flow score is only as trustworthy as the bars underneath it, and two
things can quietly corrupt it:

  FORMING BAR   A scan that runs during the session sees today's daily bar
                half-built: its volume is partial and its high/low range has
                not finished widening. Everything volume-conditioned
                (accumulation/distribution days, the volume z-score behind
                absorption, the up/down ratio) then reads today wrong.
  ZERO BARS     scan_flow.candles() drops any bar with null OHLC or zero
                volume. That is the right call -- a synthesised bar would
                feed a fabricated buy/sell split straight into the score --
                but dropping a bar silently makes two non-adjacent sessions
                look adjacent to every close-to-close indicator.

This measures both against live data instead of assuming. The number that
matters is SCORE DELTA: the same name scored with and without its last bar.
If that is small, a mid-session scan is fine; if it is large, the schedule
needs to move after the close.

    python tools/audit_bars.py                 # default sample
    python tools/audit_bars.py --limit 60      # wider sample
    python tools/audit_bars.py --symbols SPY,QQQ,AAPL
"""
import argparse, json, os, statistics, sys, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flow_lib as F
import scan_flow as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A deliberate spread: mega-cap ETFs, heavily traded names, and small caps
# where thin sessions and zero-volume days actually happen.
DEFAULT = ["SPY", "QQQ", "IWM", "DIA", "XLE", "TLT", "GLD",
           "AAPL", "MSFT", "NVDA", "AMZN", "JPM", "XOM", "KO",
           "CCL", "NCLH", "F", "PFE", "T", "INTC"]


def raw_daily(sym, rng="1y"):
    """Fetch the daily series WITHOUT scan_flow's filtering, so we can see
    exactly what was dropped and why."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
           f"?range={rng}&interval=1d&includePrePost=false")
    d = S.http_json(url)
    res = (d.get("chart", {}).get("result") or [None])[0]
    if not res:
        return None
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    ts = res.get("timestamp") or []
    blank = [None] * len(ts)
    op, hi, lo, cl, vo = (q.get(k) or blank for k in ("open", "high", "low", "close", "volume"))
    meta = res.get("meta") or {}
    return {"ts": ts, "o": op, "h": hi, "l": lo, "c": cl, "v": vo,
            "gmtoffset": meta.get("gmtoffset") or 0,
            "tz": meta.get("exchangeTimezoneName"),
            "marketPrice": meta.get("regularMarketPrice")}


def audit(sym):
    try:
        k = raw_daily(sym)
    except Exception as e:
        return {"symbol": sym, "error": str(e)[:60]}
    if not k or not k["ts"]:
        return {"symbol": sym, "error": "no data"}

    off = k["gmtoffset"]
    exch_today = (datetime.now(timezone.utc) + timedelta(seconds=off)).date()
    dates = [(datetime.fromtimestamp(t, timezone.utc) + timedelta(seconds=off)).date()
             for t in k["ts"]]

    # --- what scan_flow would drop -----------------------------------------
    kept_i, dropped = [], []
    for i in range(len(k["ts"])):
        o, h, l, c, v = k["o"][i], k["h"][i], k["l"][i], k["c"][i], k["v"][i]
        if None in (o, h, l, c):
            dropped.append((dates[i], "null OHLC")); continue
        if not v:
            dropped.append((dates[i], f"volume={v!r}")); continue
        kept_i.append(i)

    H = [k["h"][i] for i in kept_i]; L = [k["l"][i] for i in kept_i]
    C = [k["c"][i] for i in kept_i]; V = [float(k["v"][i]) for i in kept_i]
    D = [dates[i] for i in kept_i]
    if len(C) < 60:
        return {"symbol": sym, "error": f"only {len(C)} usable bars"}

    # --- is the last kept bar today's, and how formed is it? ---------------
    last_is_today = D[-1] == exch_today
    med20 = statistics.median(V[-21:-1]) if len(V) > 21 else None
    formed = (V[-1] / med20 * 100) if med20 else None

    # --- gaps introduced by dropping ---------------------------------------
    # weekend-sized gaps are normal; anything else means a session vanished
    gaps = []
    for a, b in zip(D, D[1:]):
        span = (b - a).days
        if span > 4:                       # >long-weekend
            gaps.append(f"{a}->{b} ({span}d)")

    # --- the number that matters: score with vs without the last bar -------
    full = F.analyze_daily(H, L, C, V)
    trunc = F.analyze_daily(H[:-1], L[:-1], C[:-1], V[:-1])
    if not full or not trunc:
        return {"symbol": sym, "error": "analyze_daily returned None"}

    return {
        "symbol": sym,
        "bars": len(C), "dropped": len(dropped), "dropReasons": dropped[-3:],
        "lastBar": str(D[-1]), "exchToday": str(exch_today),
        "lastIsToday": last_is_today,
        "lastVol": V[-1], "med20Vol": med20, "formedPct": formed,
        "gaps": gaps,
        "score": full["score"], "scoreExLast": trunc["score"],
        "scoreDelta": round(full["score"] - trunc["score"], 1),
        "accD": full["accDays"], "distD": full["distDays"],
        "accDx": trunc["accDays"], "distDx": trunc["distDays"],
        "absorb": full["absorption"], "absorbx": trunc["absorption"],
        "divergence": full["divergence"], "divergencex": trunc["divergence"],
        "label": full["label"], "labelExLast": trunc["label"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--limit", type=int, default=0,
                    help="also sample this many names from universe.json")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or list(DEFAULT)
    if args.limit:
        univ = json.load(open(os.path.join(ROOT, "universe.json"), encoding="utf-8"))
        names = univ["constituents"]
        step = max(1, len(names) // args.limit)
        syms += [n["yahoo"] for n in names[::step][:args.limit] if n["yahoo"] not in syms]

    now_utc = datetime.now(timezone.utc)
    et = now_utc + timedelta(hours=-4)
    mins = et.hour * 60 + et.minute
    where = ("PRE-MARKET" if mins < 570 else
             f"MID-SESSION ({(mins-570)/390*100:.0f}% through)" if mins < 960 else
             "AFTER THE CLOSE")
    print(f"audit at {now_utc:%Y-%m-%d %H:%M} UTC  =  {et:%H:%M} ET   [{where}]")
    print(f"sampling {len(syms)} symbols\n", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        rows = list(ex.map(audit, syms))

    ok = [r for r in rows if "error" not in r]
    bad = [r for r in rows if "error" in r]

    print(f"{'sym':<7}{'bars':>5}{'drop':>5}  {'last bar':<11}{'today?':<8}"
          f"{'formed':>8}  {'score':>7}{'ex-last':>8}{'delta':>7}  label")
    for r in sorted(ok, key=lambda r: -abs(r["scoreDelta"])):
        f = f"{r['formedPct']:.0f}%" if r["formedPct"] is not None else "-"
        flip = " *LABEL FLIP*" if r["label"] != r["labelExLast"] else ""
        print(f"{r['symbol']:<7}{r['bars']:>5}{r['dropped']:>5}  {r['lastBar']:<11}"
              f"{'YES' if r['lastIsToday'] else 'no':<8}{f:>8}  "
              f"{r['score']:>+7.1f}{r['scoreExLast']:>+8.1f}{r['scoreDelta']:>+7.1f}  "
              f"{r['label']}{flip}", flush=True)

    if bad:
        print("\nERRORS")
        for r in bad:
            print(f"  {r['symbol']:<7} {r['error']}")

    # ---- summary ----------------------------------------------------------
    print("\n" + "=" * 72)
    today_bars = [r for r in ok if r["lastIsToday"]]
    print(f"last bar is TODAY's (still forming): {len(today_bars)}/{len(ok)}")
    if today_bars:
        fp = [r["formedPct"] for r in today_bars if r["formedPct"] is not None]
        if fp:
            print(f"  today's volume vs 20d median: min {min(fp):.0f}%  "
                  f"median {statistics.median(fp):.0f}%  max {max(fp):.0f}%")
    deltas = [abs(r["scoreDelta"]) for r in ok]
    print(f"|score delta| from the last bar: median {statistics.median(deltas):.1f}  "
          f"p90 {sorted(deltas)[int(len(deltas)*0.9)]:.1f}  max {max(deltas):.1f}")
    flips = [r for r in ok if r["label"] != r["labelExLast"]]
    print(f"label changes caused by the last bar: {len(flips)}"
          + (f"  -> {', '.join(r['symbol'] for r in flips)}" if flips else ""))
    dz = [r for r in ok if r["dropped"]]
    print(f"names with dropped bars: {len(dz)}/{len(ok)}"
          + (f"  (max {max(r['dropped'] for r in dz)} bars)" if dz else ""))
    for r in dz[:5]:
        print(f"    {r['symbol']}: {r['dropped']} dropped, e.g. {r['dropReasons']}")
    gp = [r for r in ok if r["gaps"]]
    print(f"names with a suspicious date gap: {len(gp)}")
    for r in gp[:5]:
        print(f"    {r['symbol']}: {r['gaps'][:3]}")
    accdiff = [r for r in ok if (r["accD"], r["distD"]) != (r["accDx"], r["distDx"])]
    print(f"acc/dist day count changed by the last bar: {len(accdiff)}/{len(ok)}")
    absdiff = [r for r in ok if r["absorb"] != r["absorbx"]]
    print(f"absorption count changed by the last bar: {len(absdiff)}/{len(ok)}")
    divdiff = [r for r in ok if r["divergence"] != r["divergencex"]]
    print(f"divergence flag changed by the last bar: {len(divdiff)}/{len(ok)}"
          + (f"  -> {', '.join(r['symbol'] for r in divdiff)}" if divdiff else ""))


if __name__ == "__main__":
    main()
