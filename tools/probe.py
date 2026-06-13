#!/usr/bin/env python3
"""Local validator for the pre-market scanner (no Node required).

Mirrors the logic in netlify/functions/quotes.mjs and index.html against LIVE
Yahoo data, so you can sanity-check the scan any time — including during a real
US pre-market session (~4:00-9:30pm SGT) to confirm preMarket fields populate.

Also writes sample_quotes.json: a snapshot in the exact shape the /api/quotes
function returns, which the front-end falls back to when the proxy is
unavailable (static preview, weekends, or if Yahoo's endpoint hiccups).

    python tools/probe.py            # scan + print movers, refresh snapshot
    python tools/probe.py --no-write # don't touch sample_quotes.json
"""
import urllib.request, urllib.parse, http.cookiejar, json, sys, os, time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
BENCH = ["SPY", "QQQ", "DIA"]
FIELDS = ("symbol,shortName,longName,currency,marketState,fullExchangeName,"
          "regularMarketPrice,regularMarketChangePercent,regularMarketVolume,"
          "regularMarketPreviousClose,preMarketPrice,preMarketChangePercent,"
          "postMarketPrice,postMarketChangePercent,marketCap,"
          "averageDailyVolume3Month,averageDailyVolume10Day")

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def _get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    return opener.open(req, timeout=25)

def yahoo_auth():
    # fc.yahoo.com 404s but sets the A1/A3 consent cookies the crumb endpoint needs.
    for url in ("https://fc.yahoo.com", "https://finance.yahoo.com/"):
        try: _get(url)
        except Exception: pass
    crumb = _get("https://query1.finance.yahoo.com/v1/test/getcrumb").read().decode().strip()
    if not crumb or "<" in crumb:
        raise RuntimeError("crumb fetch failed")
    return crumb

def num(v):
    return v if isinstance(v, (int, float)) else None

def normalize(q):
    return {
        "symbol": q.get("symbol"),
        "name": q.get("shortName") or q.get("longName") or q.get("symbol"),
        "currency": q.get("currency", "USD"),
        "exchange": q.get("fullExchangeName"),
        "marketState": q.get("marketState", "UNKNOWN"),
        "price": num(q.get("regularMarketPrice")),
        "prevClose": num(q.get("regularMarketPreviousClose")),
        "regChangePct": num(q.get("regularMarketChangePercent")),
        "regVolume": num(q.get("regularMarketVolume")),
        "avgVol3m": num(q.get("averageDailyVolume3Month")),
        "avgVol10d": num(q.get("averageDailyVolume10Day")),
        "marketCap": num(q.get("marketCap")),
        "preMarketPrice": num(q.get("preMarketPrice")),
        "preMarketChangePct": num(q.get("preMarketChangePercent")),
        "postMarketPrice": num(q.get("postMarketPrice")),
        "postMarketChangePct": num(q.get("postMarketChangePercent")),
    }

def fetch_quotes(symbols, crumb):
    out, BATCH = [], 100
    for i in range(0, len(symbols), BATCH):
        group = symbols[i:i + BATCH]
        url = ("https://query1.finance.yahoo.com/v7/finance/quote"
               f"?symbols={urllib.parse.quote(','.join(group))}&fields={FIELDS}"
               f"&crumb={urllib.parse.quote(crumb)}")
        data = json.loads(_get(url).read().decode())  # cookie jar auto-attaches A1/A3
        for q in data.get("quoteResponse", {}).get("result", []):
            out.append(normalize(q))
        time.sleep(0.15)
    return out

def active_change(q):
    """Auto-basis active change %, matching the front-end."""
    st = q["marketState"]
    if st == "PRE":
        return (q["preMarketChangePct"] if q["preMarketChangePct"] is not None else q["regChangePct"]), "PRE"
    if st in ("POST", "POSTPOST"):
        return (q["postMarketChangePct"] if q["postMarketChangePct"] is not None else q["regChangePct"]), "POST"
    return q["regChangePct"], "REG"

def main():
    write = "--no-write" not in sys.argv
    with open(os.path.join(ROOT, "universe.json"), encoding="utf-8") as f:
        univ = json.load(f)
    meta = {u["yahoo"]: u for u in univ["constituents"]}
    symbols = list(dict.fromkeys([u["yahoo"] for u in univ["constituents"]] + BENCH))

    print(f"Universe: {len(symbols)} symbols ({univ['source']})")
    crumb = yahoo_auth()
    print(f"Auth OK  crumb={crumb!r}  cookies={[c.name for c in cj]}")
    t0 = time.time()
    quotes = fetch_quotes(symbols, crumb)
    qmap = {q["symbol"]: q for q in quotes}
    print(f"Fetched {len(quotes)}/{len(symbols)} quotes in {time.time()-t0:.1f}s")

    state = (qmap.get("SPY") or qmap.get("QQQ") or {}).get("marketState", "?")
    print(f"marketState = {state}")

    # missing symbols (Yahoo mismatch / delisted)
    missing = [s for s in symbols if s not in qmap]
    if missing:
        print(f"WARN: {len(missing)} symbols returned no quote: {missing[:12]}{'…' if len(missing)>12 else ''}")

    spy, _ = active_change(qmap["SPY"]) if "SPY" in qmap else (None, None)
    rows = []
    for s, q in qmap.items():
        if s in BENCH:
            continue
        chg, sess = active_change(q)
        relvol = (q["regVolume"] / q["avgVol3m"]) if q["regVolume"] and q["avgVol3m"] else None
        rows.append({
            "sym": meta.get(s, {}).get("symbol", s), "name": q["name"],
            "sector": meta.get(s, {}).get("sector", "?"),
            "chg": chg, "sess": sess, "price": q["price"], "vol": q["regVolume"],
            "relVol": relvol, "cap": q["marketCap"],
            "vsSpy": (chg - spy) if (chg is not None and spy is not None) else None,
        })

    def show(title, items, valfn):
        print(f"\n{title}")
        for r in items:
            d = f"{r['chg']:+.2f}%" if r["chg"] is not None else "  n/a"
            print(f"  {r['sym']:<6} {d:>8}  {valfn(r):>12}  {r['sector']}")

    valid = [r for r in rows if r["chg"] is not None]
    fcap = lambda r: ("$%.1fB" % (r["cap"]/1e9)) if r["cap"] else "–"
    fvol = lambda r: ("%.1fM" % (r["vol"]/1e6)) if r["vol"] else "–"
    fdev = lambda r: (f"{r['vsSpy']:+.2f}% vs SPY" if r["vsSpy"] is not None else "–")
    show("TOP GAINERS",  sorted(valid, key=lambda r: r["chg"], reverse=True)[:8], fcap)
    show("TOP LOSERS",   sorted(valid, key=lambda r: r["chg"])[:8], fcap)
    show("MOST ACTIVE",  sorted([r for r in rows if r["vol"]], key=lambda r: r["vol"], reverse=True)[:8], fvol)
    show("BIGGEST DEVIATION vs SPY",
         sorted([r for r in valid if r["vsSpy"] is not None], key=lambda r: abs(r["vsSpy"]), reverse=True)[:8], fdev)

    if write:
        payload = {"provider": "yahoo-probe", "count": len(quotes),
                   "asOf": datetime.now(timezone.utc).isoformat(), "quotes": quotes}
        out = os.path.join(ROOT, "sample_quotes.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        print(f"\nWrote snapshot -> {out}")

if __name__ == "__main__":
    main()
