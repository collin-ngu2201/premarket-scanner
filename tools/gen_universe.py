#!/usr/bin/env python3
"""Build universe.json from the public S&P 500 constituents list.

Re-run periodically to pick up index add/drops. To widen the universe
(Nasdaq-100, Russell 1000), append more sources below or hand-edit universe.json.
Yahoo uses dashes for class shares (BRK.B -> BRK-B), handled here.

    python tools/gen_universe.py
"""
import urllib.request, csv, io, json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
SRC = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

def main():
    raw = urllib.request.urlopen(
        urllib.request.Request(SRC, headers={"User-Agent": UA}), timeout=30
    ).read().decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(raw)))

    out = []
    for r in rows:
        sym = r["Symbol"].strip()
        out.append({
            "symbol": sym,
            "yahoo": sym.replace(".", "-"),
            "name": r["Security"].strip(),
            "sector": r["GICS Sector"].strip(),
            "industry": r["GICS Sub-Industry"].strip(),
        })

    payload = {"source": "S&P 500 (datahub constituents)", "count": len(out), "constituents": out}
    path = os.path.join(ROOT, "universe.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=0, ensure_ascii=False)
    print(f"wrote {path} with {len(out)} names")

if __name__ == "__main__":
    main()
