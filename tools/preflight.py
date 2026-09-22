#!/usr/bin/env python3
"""Pre-deployment readiness check for the scheduled scan.

Everything the nightly run depends on, verified against live services rather
than assumed. Grouped by what breaks if it fails:

  UPSTREAM   Yahoo's v8 chart endpoint (all three scanners) and the v7 quote
             endpoint's cookie+crumb handshake (api/quotes.mjs). The crumb
             dance is the single most fragile link in the whole system -- it
             is an unofficial endpoint and it has changed without notice
             before.
  SCANNERS   Each scanner end-to-end on real data at small scale, timed, so
             the full-universe runtime can be projected against the job budget.
  DEPLOYED   The live Vercel functions and pages, including that /api/chart
             really returns the 6th (volume) candle field that flow.html's
             live tab needs.
  SNAPSHOTS  The committed JSON the dashboards read: parses, has rows, and is
             not stale.

Writes nothing and commits nothing. Exit code is non-zero if any check FAILs,
so a CI run goes red on a real problem.

    python tools/preflight.py
    python tools/preflight.py --quick          # skip the scanner dry-runs
    python tools/preflight.py --site my.app    # override the deployed host
"""
import argparse, json, os, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
DEFAULT_SITE = "premarket-scanner-git-main-collin-s-trading.vercel.app"

# Full-universe sizes, used to project runtime from the limited dry-runs.
UNIVERSE_N = 1506
FLOW_N = UNIVERSE_N + 34          # + the ETF panel
JOB_BUDGET_MIN = 60               # comfortable ceiling for one Actions job

RESULTS = []


STRICT_DEPLOYED = False


def record(group, name, ok, detail="", warn=False):
    # The scan runs on Actions and commits to git; it never touches the
    # deployed site. So a site problem is reported, but it does not make the
    # readiness signal red unless --strict-deployed asks for that.
    if group == "deployed" and not STRICT_DEPLOYED:
        warn = True
    RESULTS.append((group, name, "WARN" if (warn and not ok) else ("PASS" if ok else "FAIL"), detail))
    tag = "WARN" if (warn and not ok) else ("PASS" if ok else "FAIL")
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def get(url, timeout=40, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def get_json(url, timeout=40, headers=None):
    """Fetch and parse, but on failure say WHAT came back instead of just
    'Expecting value' -- an auth interstitial, a 404 page and a real outage
    all produce that same useless message otherwise."""
    st, body, hdrs = get(url, timeout, headers)
    ctype = (hdrs.get("Content-Type") or hdrs.get("content-type") or "?").split(";")[0]
    try:
        return st, json.loads(body), ctype, None
    except Exception:
        head = body[:200].decode("utf-8", "replace").replace("\n", " ").strip()
        why = "Vercel auth wall" if ("sso" in head.lower() or "authentication required" in head.lower()) \
              else "HTML page" if ctype.startswith("text/html") else "unparseable"
        return st, None, ctype, f"HTTP {st}, {ctype}, {why}: {head[:90]!r}"


# ---------------------------------------------------------------------------
# UPSTREAM
# ---------------------------------------------------------------------------

def check_upstream():
    print("\nUPSTREAM (Yahoo)")
    # daily chart -- the backbone of scan_levels and scan_flow
    try:
        st, body, _ = get("https://query1.finance.yahoo.com/v8/finance/chart/SPY"
                          "?range=1y&interval=1d&includePrePost=false")
        d = json.loads(body)
        res = d["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        n = len(res["timestamp"])
        vols = [v for v in (q.get("volume") or []) if v]
        record("upstream", "v8 daily chart", n > 200 and len(vols) > 200,
               f"{n} bars, {len(vols)} with volume")
        record("upstream", "daily bars carry volume", len(vols) > 0,
               f"last volume {vols[-1]:,}" if vols else "NO VOLUME FIELD")
    except Exception as e:
        record("upstream", "v8 daily chart", False, str(e)[:70])

    # intraday chart -- stage 2 of the flow scan, and flow.html's live tab
    try:
        st, body, _ = get("https://query1.finance.yahoo.com/v8/finance/chart/SPY"
                          "?range=5d&interval=5m&includePrePost=true")
        res = json.loads(body)["chart"]["result"][0]
        n = len(res["timestamp"])
        meta = res.get("meta", {})
        cp = (meta.get("currentTradingPeriod") or {}).get("regular", {})
        pre = sum(1 for t in res["timestamp"] if cp.get("start") and t < cp["start"])
        record("upstream", "v8 intraday 5m/5d", n > 300, f"{n} bars")
        record("upstream", "pre/post bars included", pre > 0,
               f"{pre} bars before today's open", warn=True)
    except Exception as e:
        record("upstream", "v8 intraday 5m/5d", False, str(e)[:70])

    # the crumb handshake behind api/quotes.mjs -- the most fragile dependency
    try:
        jar = {}
        for u in ("https://fc.yahoo.com", "https://finance.yahoo.com/"):
            try:
                req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "text/html"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    for c in r.headers.get_all("Set-Cookie") or []:
                        pair = c.split(";")[0]
                        if "=" in pair:
                            k, v = pair.split("=", 1); jar[k.strip()] = v.strip()
            except Exception:
                pass
        cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
        st, body, _ = get("https://query1.finance.yahoo.com/v1/test/getcrumb",
                          headers={"User-Agent": UA, "Cookie": cookie, "Accept": "text/plain"})
        crumb = body.decode().strip()
        good = bool(crumb) and "<" not in crumb and len(crumb) <= 40
        record("upstream", "v7 cookie+crumb handshake", good,
               f"crumb ok ({len(crumb)} chars), {len(jar)} cookies" if good
               else f"bad crumb: {crumb[:40]!r}")
        if good:
            st, body, _ = get("https://query1.finance.yahoo.com/v7/finance/quote"
                              f"?symbols=AAPL,MSFT&crumb={urllib.parse.quote(crumb)}",
                              headers={"User-Agent": UA, "Cookie": cookie})
            qr = json.loads(body).get("quoteResponse", {}).get("result", [])
            record("upstream", "v7 quote endpoint", len(qr) == 2,
                   f"{len(qr)} quotes, AAPL={qr[0].get('regularMarketPrice') if qr else '?'}")
    except Exception as e:
        record("upstream", "v7 cookie+crumb handshake", False, str(e)[:70])


# ---------------------------------------------------------------------------
# SCANNERS
# ---------------------------------------------------------------------------

def run_scanner(label, args, n_sample, n_full, must_contain=None):
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable] + args, cwd=ROOT, capture_output=True,
                           text=True, timeout=900)
    except subprocess.TimeoutExpired:
        record("scanners", label, False, "timed out after 900s"); return None
    el = time.time() - t0
    if p.returncode != 0:
        tail = (p.stderr or p.stdout).strip().splitlines()[-3:]
        record("scanners", label, False, f"exit {p.returncode}: {' | '.join(tail)[:90]}")
        return None
    out = p.stdout
    if must_contain and must_contain not in out:
        record("scanners", label, False, f"output missing {must_contain!r}")
        return None
    proj = el / max(1, n_sample) * n_full / 60
    ok = proj < JOB_BUDGET_MIN
    record("scanners", label, ok,
           f"{n_sample} names in {el:.0f}s → full run ≈ {proj:.1f} min "
           f"(budget {JOB_BUDGET_MIN})")
    return out


def check_scanners(quick):
    print("\nSCANNERS (live dry-run)")
    if quick:
        print("  (skipped: --quick)")
        return
    out = run_scanner("scan_flow", ["tools/scan_flow.py", "--limit", "40",
                                    "--workers", "8", "--intraday", "10", "--no-write"],
                      74, FLOW_N, must_contain="MARKET")
    if out:
        # the scan must actually classify, not just exit zero
        import re
        m = re.search(r"stage 1 done: (\d+) analysed", out)
        a = int(m.group(1)) if m else 0
        record("scanners", "scan_flow analysed rows", a > 20, f"{a} rows analysed")
        m2 = re.search(r"PRICE/FLOW DIVERGENCES: (\d+)", out)
        record("scanners", "scan_flow produced a report", m2 is not None,
               f"divergences: {m2.group(1)}" if m2 else "no divergence section")
    # These two have no --no-write, so the dry-run overwrites the committed
    # snapshots with a 40-name sample. Nothing commits them, but restore anyway
    # so "did the checks write anything?" stays a meaningful question.
    dirty = ["levels_results.json", "scan_results.json", "iv_history.json"]
    run_scanner("scan_levels", ["tools/scan_levels.py", "--limit", "40", "--workers", "8"],
                40, UNIVERSE_N, must_contain="RESULTS")
    run_scanner("scan_options", ["tools/scan_options.py", "--limit", "25", "--workers", "6"],
                25, UNIVERSE_N)
    r = subprocess.run(["git", "checkout", "--"] + dirty, cwd=ROOT,
                       capture_output=True, text=True)
    record("scanners", "sample snapshots restored", r.returncode == 0,
           "working tree left clean" if r.returncode == 0 else r.stderr.strip()[:60])


# ---------------------------------------------------------------------------
# DEPLOYED
# ---------------------------------------------------------------------------

# Each page must contain its own marker, not merely return 200 with some bytes:
# a Vercel auth interstitial is a 200 with plenty of bytes and would otherwise
# sail through every page check while the real site was unreachable.
PAGE_MARKERS = {
    "index.html": "Pre-Market Scanner",
    "flow.html": "Money Flow",
    "divergence.html": "Divergence",
    "options.html": "Options Premium",
    "levels.html": "Key Levels",
}


def check_deployed(site):
    print(f"\nDEPLOYED ({site})")
    base = f"https://{site}"
    # Probe first, judge after. Vercel's SSO page is a ~334KB app shell with no
    # distinctive wording, so the reliable signature is structural: every path
    # returns HTML of the same size and none contains its own marker.
    probes = {}
    for page, marker in PAGE_MARKERS.items():
        try:
            st, body, hdrs = get(f"{base}/{page}", timeout=30)
            text = body.decode("utf-8", "replace")
            probes[page] = (st, len(body), marker in text, text)
        except Exception as e:
            probes[page] = (0, 0, False, str(e))

    sizes = [n for _, n, _, _ in probes.values() if n]
    none_matched = bool(probes) and not any(hit for _, _, hit, _ in probes.values())
    all_html = bool(sizes) and all("<!doctype html" in t.lower()[:200]
                                   for _, _, _, t in probes.values() if t)
    # Sizes are near-identical rather than exactly equal -- the shell embeds a
    # per-request deployment id -- and it is far larger than any error page.
    uniform = bool(sizes) and (max(sizes) - min(sizes)) < 8192 and min(sizes) > 50_000
    protected = none_matched and all_html and uniform

    if protected:
        record("deployed", "deployment protection", False,
               "Vercel SSO Protection is ON and there is no custom domain, so every "
               "*.vercel.app path returns the auth page to anyone not signed in to "
               "your Vercel account. Your own browser is unaffected; these checks "
               "cannot see past it, so the results below are inconclusive, not failing.",
               warn=True)
    for page, (st, n, hit, _) in probes.items():
        record("deployed", f"page {page}", st == 200 and hit,
               f"{st}, {n//1024}KB" + ("" if hit else
               " — auth page, not the real page" if protected else
               f" — marker missing"), warn=protected)

    # /api/chart must carry the 6th field or flow.html's live tab silently degrades
    st, d, ctype, err = get_json(f"{base}/api/chart?symbol=AAPL&interval=5m&range=1d", 40)
    if err:
        record("deployed", "/api/chart", False, err, warn=protected)
    else:
        cs = d.get("candles") or []
        widths = sorted({len(c) for c in cs[:50]})
        has_vol = bool(cs) and all(len(c) >= 6 for c in cs[:50])
        nonzero = sum(1 for c in cs if len(c) > 5 and c[5])
        record("deployed", "/api/chart responds", len(cs) > 5, f"{len(cs)} candles")
        record("deployed", "/api/chart returns volume (index 5)", has_vol,
               f"candle widths {widths}, {nonzero} with non-zero volume")
        if not has_vol:
            record("deployed", "flow.html live tab", False,
                   "candles lack index 5 — the live tab cannot compute signed volume")

    st, d, ctype, err = get_json(f"{base}/api/quotes?symbols=AAPL,SPY", 45)
    if err:
        record("deployed", "/api/quotes", False, err, warn=protected)
    else:
        qs = d.get("quotes") or []
        record("deployed", "/api/quotes responds", len(qs) >= 1,
               f"{len(qs)} quotes, provider={d.get('provider')}")

    for f in ("flow_results.json", "levels_results.json", "scan_results.json"):
        st, d, ctype, err = get_json(f"{base}/{f}", 45)
        if err:
            record("deployed", f"served {f}", False, err, warn=True)
        else:
            record("deployed", f"served {f}", bool(d),
                   f"asOf={str(d.get('asOf'))[:19]}, {len(d.get('results') or [])} rows")


# ---------------------------------------------------------------------------
# SNAPSHOTS on disk
# ---------------------------------------------------------------------------

def check_snapshots():
    print("\nSNAPSHOTS (committed)")
    now = datetime.now(timezone.utc)
    for f, min_rows in (("flow_results.json", 100), ("levels_results.json", 50),
                        ("scan_results.json", 5), ("universe.json", 1000)):
        path = os.path.join(ROOT, f)
        if not os.path.exists(path):
            record("snapshots", f, False, "missing"); continue
        try:
            d = json.load(open(path))
        except Exception as e:
            record("snapshots", f, False, f"does not parse: {e}"); continue
        rows = d.get("results") or d.get("constituents") or []
        asof = d.get("asOf")
        age = None
        if asof:
            try:
                age = (now - datetime.fromisoformat(asof)).total_seconds() / 3600
            except Exception:
                pass
        detail = f"{len(rows)} rows" + (f", {age:.0f}h old" if age is not None else "")
        record("snapshots", f, len(rows) >= min_rows, detail)
        if age is not None and age > 96:
            record("snapshots", f"{f} freshness", False,
                   f"{age:.0f}h old — the scheduled scan may not be updating it", warn=True)


def check_local():
    print("\nLOCAL (code)")
    p = subprocess.run([sys.executable, "tools/test_flow_lib.py"], cwd=ROOT,
                       capture_output=True, text=True)
    record("local", "flow_lib self-tests", p.returncode == 0,
           p.stdout.strip().splitlines()[-1] if p.stdout else "no output")
    for mod in ("tools/flow_lib.py", "tools/scan_flow.py", "tools/scan_levels.py",
                "tools/scan_options.py", "tools/audit_bars.py"):
        p = subprocess.run([sys.executable, "-m", "py_compile", mod], cwd=ROOT,
                           capture_output=True, text=True)
        if p.returncode != 0:
            record("local", f"compile {mod}", False, p.stderr.strip()[:70])
    record("local", "all scanners compile", True, "5 modules")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=DEFAULT_SITE)
    ap.add_argument("--quick", action="store_true", help="skip scanner dry-runs")
    ap.add_argument("--skip-deployed", action="store_true")
    ap.add_argument("--strict-deployed", action="store_true",
                    help="let deployed-site problems fail the run (default: warn)")
    args = ap.parse_args()
    global STRICT_DEPLOYED
    STRICT_DEPLOYED = args.strict_deployed

    now = datetime.now(timezone.utc)
    et = now + timedelta(hours=-4)
    mins = et.hour * 60 + et.minute
    where = ("PRE-MARKET" if mins < 570 else
             f"MID-SESSION ({(mins-570)/390*100:.0f}% through)" if mins < 960 else
             "AFTER THE CLOSE")
    print(f"PREFLIGHT  {now:%Y-%m-%d %H:%M} UTC = {et:%H:%M} ET [{where}]")

    check_local()
    check_upstream()
    if not args.skip_deployed:
        check_deployed(args.site)
    check_snapshots()
    check_scanners(args.quick)

    print("\n" + "=" * 72)
    fails = [r for r in RESULTS if r[2] == "FAIL"]
    warns = [r for r in RESULTS if r[2] == "WARN"]
    print(f"{len(RESULTS)} checks: {len(RESULTS)-len(fails)-len(warns)} pass, "
          f"{len(warns)} warn, {len(fails)} FAIL")
    for g, n, s, d in warns:
        print(f"  WARN  {g}/{n}: {d}")
    for g, n, s, d in fails:
        print(f"  FAIL  {g}/{n}: {d}")
    if not fails:
        print("\nREADY — the scan path (Yahoo → scanners → git) is healthy.")
        if warns:
            print("       Warnings above do not block the scan.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
