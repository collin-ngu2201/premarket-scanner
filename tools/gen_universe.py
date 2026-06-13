#!/usr/bin/env python3
"""Build universe.json from the S&P Composite 1500 (S&P 500 + 400 + 600).

All three Wikipedia constituent tables use the same GICS sector taxonomy, so
the scanner's sector filter stays consistent across large/mid/small caps.
Re-run periodically to pick up index add/drops.

    python tools/gen_universe.py

Falls back to the datahub S&P 500 CSV if Wikipedia parsing fails, so the app
always has a usable universe. Parses HTML with bs4 + the stdlib html.parser
(no lxml/html5lib needed).
"""
import csv, io, json, os, re
import requests
from bs4 import BeautifulSoup
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TICKER_RE = re.compile(r"[A-Z][A-Z.\-]*")

SOURCES = [
    ("sp500", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"),
    ("sp400", "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"),
    ("sp600", "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"),
]


def col_index(headers, *cands):
    for cand in cands:
        for i, h in enumerate(headers):
            if cand.lower() in h.lower():
                return i
    return None


def parse_table(table, idx):
    rows = table.find_all("tr")
    if not rows:
        return None
    headers = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
    si = col_index(headers, "Symbol", "Ticker")
    sec = col_index(headers, "GICS Sector", "Sector")
    if si is None or sec is None:
        return None
    nmi = col_index(headers, "Security", "Company", "Name")
    subi = col_index(headers, "Sub-Industry", "Sub Industry")

    out = []
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) <= max(si, sec):
            continue
        raw = cells[si].get_text(" ", strip=True).upper()
        m = TICKER_RE.match(raw)
        if not m:
            continue
        sym = m.group(0)
        out.append({
            "symbol": sym,
            "yahoo": sym.replace(".", "-"),
            "name": cells[nmi].get_text(" ", strip=True) if nmi is not None and len(cells) > nmi else sym,
            "sector": cells[sec].get_text(" ", strip=True),
            "industry": cells[subi].get_text(" ", strip=True) if subi is not None and len(cells) > subi else "",
            "index": idx,
        })
    return out or None


def parse_index(idx, url):
    html = requests.get(url, headers={"User-Agent": UA}, timeout=40).text
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.select("table.wikitable"):
        rows = parse_table(table, idx)
        if rows:
            print(f"  [{idx}] {len(rows)} names")
            return rows
    print(f"  [{idx}] no constituents table found")
    return []


def from_datahub():
    url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
    raw = requests.get(url, headers={"User-Agent": UA}, timeout=30).text
    out = []
    for r in csv.DictReader(io.StringIO(raw)):
        s = r["Symbol"].strip().upper()
        out.append({"symbol": s, "yahoo": s.replace(".", "-"), "name": r["Security"].strip(),
                    "sector": r["GICS Sector"].strip(), "industry": r["GICS Sub-Industry"].strip(),
                    "index": "sp500"})
    return out


def main():
    merged = {}
    for idx, url in SOURCES:
        try:
            rows = parse_index(idx, url)
        except Exception as e:
            print(f"  [{idx}] FAILED: {e}")
            rows = []
        for e in rows:
            merged.setdefault(e["symbol"], e)   # 500 > 400 > 600 precedence

    if len(merged) < 600:
        print(f"WARN: only {len(merged)} names parsed; falling back to datahub S&P 500")
        merged = {}
        for e in from_datahub():
            merged.setdefault(e["symbol"], e)

    constituents = sorted(merged.values(), key=lambda e: e["symbol"])
    src = ("S&P Composite 1500 (S&P 500+400+600, Wikipedia/GICS)"
           if len(constituents) > 600 else "S&P 500 (datahub)")
    payload = {"source": src, "count": len(constituents), "constituents": constituents}

    path = os.path.join(ROOT, "universe.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=0, ensure_ascii=False)

    byidx = Counter(e["index"] for e in constituents)
    bysec = Counter(e["sector"] for e in constituents)
    print(f"\nwrote {path}: {len(constituents)} names - {dict(byidx)}")
    print("sectors:", dict(sorted(bysec.items())))


if __name__ == "__main__":
    main()
