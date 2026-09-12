#!/usr/bin/env python3
"""Self-tests for tools/flow_lib.py -- run with: python tools/test_flow_lib.py

Every case is a synthetic tape whose answer is known by construction, so the
suite needs no network and no market data. The two end-to-end cases matter
most: a textbook accumulation tape must score positive and a textbook
distribution tape negative, or the composite is worthless.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flow_lib as F

FAILED = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(f"{name}: {detail}")


def near(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


# ---------------------------------------------------------------------------
# synthetic tape builders
# ---------------------------------------------------------------------------

def tape(n, drift, close_in_range, vol=1_000_000, start=100.0, rng_pct=0.02,
         vol_jitter=None):
    """Build OHLCV bars with an independently controlled drift and close location.

    The close follows the drift; the bar's high/low are then placed AROUND that
    close so the close lands at `close_in_range` of the bar's range:

        0.0 = every bar closes on its low (sellers had the last word)
        1.0 = every bar closes on its high
        0.5 = mid-range

    By construction CLV = 2*close_in_range - 1, so a tape can rise while every
    bar closes on its low -- the exact "price up, flow down" case the
    divergence detector exists to catch. The range is widened when it has to be
    so the bar still contains the previous close (a real bar that gaps up and
    closes on its low is a wide bar too).
    """
    H, L, C, V, O = [], [], [], [], []
    px = start
    cir = min(max(close_in_range, 0.0), 1.0)
    for i in range(n):
        prev = px
        c = prev * (1 + drift)
        r = c * rng_pct
        need = abs(c - prev)
        if c > prev and cir > 0:
            r = max(r, need / cir * 1.05)
        elif c < prev and cir < 1:
            r = max(r, need / (1 - cir) * 1.05)
        l = c - r * cir
        h = c + r * (1 - cir)
        v = vol if vol_jitter is None else vol * vol_jitter[i % len(vol_jitter)]
        O.append(min(max(prev, l), h)); H.append(h); L.append(l); C.append(c); V.append(v)
        px = c
    return O, H, L, C, V


# ---------------------------------------------------------------------------
# 1. signed-volume estimators
# ---------------------------------------------------------------------------

def test_splits():
    # CLV: close on the high -> all buy; on the low -> all sell; mid -> even.
    b, s = F.clv_split([10.0], [8.0], [10.0], [500.0])
    check("clv close-on-high", near(b[0], 500.0) and near(s[0], 0.0), f"{b} {s}")
    b, s = F.clv_split([10.0], [8.0], [8.0], [500.0])
    check("clv close-on-low", near(b[0], 0.0) and near(s[0], 500.0), f"{b} {s}")
    b, s = F.clv_split([10.0], [8.0], [9.0], [500.0])
    check("clv close-mid", near(b[0], 250.0) and near(s[0], 250.0), f"{b} {s}")
    # zero-range bar must not divide by zero
    b, s = F.clv_split([5.0], [5.0], [5.0], [100.0])
    check("clv zero range", near(b[0], 50.0) and near(s[0], 50.0), f"{b} {s}")

    # tick rule: sign follows close-to-close, zero ticks carry forward
    closes = [10, 11, 11, 10, 10]
    v = [100.0] * 5
    b, s = F.tick_split(closes, v)
    check("tick up bar", near(b[1], 100.0), str(b))
    check("tick zero-tick carries up", near(b[2], 100.0), str(b))
    check("tick down bar", near(s[3], 100.0), str(s))
    check("tick zero-tick carries down", near(s[4], 100.0), str(s))

    # BVC: conserves volume, and a big up move against quiet history is
    # classified as overwhelmingly buy.
    closes = [100 + 0.01 * i for i in range(60)] + [105.0]
    vols = [1000.0] * 61
    b, s = F.bvc_split(closes, vols)
    check("bvc conserves volume",
          all(near(b[i] + s[i], vols[i], 1e-6) for i in range(61)))
    check("bvc big up bar is mostly buy", b[-1] / 1000.0 > 0.99, str(b[-1]))
    closes2 = closes[:-1] + [95.0]
    b2, s2 = F.bvc_split(closes2, vols)
    check("bvc big down bar is mostly sell", s2[-1] / 1000.0 > 0.99, str(s2[-1]))
    # a flat bar must split ~50/50 -- the honest answer when nothing happened
    closes3 = [100.0 + (0.5 if i % 2 else -0.5) for i in range(60)] + [100.0]
    b3, _ = F.bvc_split(closes3, vols)
    check("bvc flat bar ~50/50", 0.3 < b3[-1] / 1000.0 < 0.7, str(b3[-1]))

    # no-history guard: first bars fall back to 50/50 rather than blowing up
    b4, s4 = F.bvc_split([10.0, 11.0], [100.0, 100.0])
    check("bvc short series safe", near(b4[0], 50.0) and near(b4[1], 50.0), f"{b4}")


def test_agreement():
    # a clean accumulation tape: all three estimators should agree on "buying"
    _, H, L, C, V = tape(60, 0.004, 0.9)
    n, d = F.method_agreement(H, L, C, V)
    check("agreement bullish tape", n == 3 and d == 1, f"n={n} d={d}")
    _, H, L, C, V = tape(60, -0.004, 0.1)
    n, d = F.method_agreement(H, L, C, V)
    check("agreement bearish tape", n == 3 and d == -1, f"n={n} d={d}")


# ---------------------------------------------------------------------------
# 2. CVD + divergence
# ---------------------------------------------------------------------------

def test_cvd_and_divergence():
    check("cvd cumulates", F.cvd([3, 3, 3], [1, 1, 1]) == [2, 4, 6])

    # price grinds UP while every bar closes on its low on heavy volume:
    # the classic "rallies are being sold" footprint -> bearish divergence.
    _, H, L, C, V = tape(40, 0.004, 0.05)
    b, s = F.clv_split(H, L, C, V)
    d = F.cvd_divergence(C, F.cvd(b, s), 20)
    check("bearish divergence detected", d["kind"] == -1, str(d))

    # price drifts DOWN while every bar closes on its high -> dips bought.
    _, H, L, C, V = tape(40, -0.004, 0.95)
    b, s = F.clv_split(H, L, C, V)
    d = F.cvd_divergence(C, F.cvd(b, s), 20)
    check("bullish divergence detected", d["kind"] == 1, str(d))

    # price and flow moving together must NOT flag a divergence
    _, H, L, C, V = tape(40, 0.004, 0.95)
    b, s = F.clv_split(H, L, C, V)
    d = F.cvd_divergence(C, F.cvd(b, s), 20)
    check("no false divergence when aligned", d["kind"] == 0, str(d))

    check("divergence short series safe",
          F.cvd_divergence([1, 2], [1, 2], 20)["kind"] == 0)

    # Regression: CVD is a running total, so normalising its move by its
    # absolute LEVEL made the test progressively blinder the more history
    # preceded the window -- the identical pattern flagged at 120 bars and
    # went silent at 300. Normalising by the in-window range fixes it, and the
    # same tape must now be detected at every length.
    for n in (60, 120, 200, 300, 500):
        _, H, L, C, V = tape(n, 0.004, 0.05)
        b, s_ = F.clv_split(H, L, C, V)
        d = F.cvd_divergence(C, F.cvd(b, s_), 20)
        check(f"divergence detected regardless of history ({n} bars)",
              d["kind"] == -1, f"{n} bars -> {d}")

    # ...and the normalisation must not depend on where the cumulation started
    _, H, L, C, V = tape(200, 0.004, 0.05)
    b, s_ = F.clv_split(H, L, C, V)
    base = F.cvd(b, s_)
    shifted = [x + 5e9 for x in base]          # same shape, huge offset
    check("divergence is invariant to a CVD offset",
          F.cvd_divergence(C, base, 20)["kind"] == F.cvd_divergence(C, shifted, 20)["kind"]
          and near(F.cvd_divergence(C, base, 20)["cvdChg"],
                   F.cvd_divergence(C, shifted, 20)["cvdChg"], 1e-9))

    # a flat CVD must not divide by zero
    check("divergence flat cvd safe",
          F.cvd_divergence([100 + i for i in range(30)], [7.0] * 30, 20)["kind"] == 0)


# ---------------------------------------------------------------------------
# 3. VPIN
# ---------------------------------------------------------------------------

def test_vpin():
    n = 200
    vols = [1000.0] * n
    # perfectly one-sided flow -> maximum toxicity, positive direction
    buy, sell = [1000.0] * n, [0.0] * n
    r = F.vpin(buy, sell, vols, bucket_volume=5000, n_buckets=20)
    check("vpin one-sided = 1", near(r["vpin"], 1.0, 1e-9), str(r))
    check("vpin signed positive", near(r["signed"], 1.0, 1e-9), str(r))
    r = F.vpin(sell, buy, vols, bucket_volume=5000, n_buckets=20)
    check("vpin signed negative", near(r["signed"], -1.0, 1e-9), str(r))

    # evenly balanced flow -> zero toxicity
    buy = [500.0] * n
    r = F.vpin(buy, buy, vols, bucket_volume=5000, n_buckets=20)
    check("vpin balanced = 0", near(r["vpin"], 0.0, 1e-9), str(r))

    # bucketing is volume-synchronised, not time-synchronised: 200k total
    # volume in 5k buckets must produce exactly 40 buckets regardless of how
    # that volume is distributed across bars.
    check("vpin bucket count", r["buckets"] == 40, str(r["buckets"]))
    lumpy = [100.0] * 100 + [1900.0] * 100
    lb, ls = [v / 2 for v in lumpy], [v / 2 for v in lumpy]
    r2 = F.vpin(lb, ls, lumpy, bucket_volume=5000, n_buckets=20)
    check("vpin bucket count lumpy", r2["buckets"] == 40, str(r2["buckets"]))

    check("vpin bad bucket size safe", F.vpin([1], [1], [1], 0)["vpin"] is None)


# ---------------------------------------------------------------------------
# 4. classical indicators
# ---------------------------------------------------------------------------

def test_classics():
    closes = [10, 11, 10, 12, 12]
    vols = [100.0] * 5
    check("obv", F.obv(closes, vols) == [0, 100, 0, 100, 100],
          str(F.obv(closes, vols)))

    _, H, L, C, V = tape(40, 0.002, 1.0)      # every close on the high
    c = F.cmf(H, L, C, V, 21)
    check("cmf closes-on-high ~ +1", c[-1] is not None and c[-1] > 0.95, str(c[-1]))
    _, H, L, C, V = tape(40, -0.002, 0.0)
    c = F.cmf(H, L, C, V, 21)
    check("cmf closes-on-low ~ -1", c[-1] is not None and c[-1] < -0.95, str(c[-1]))

    _, H, L, C, V = tape(40, 0.004, 0.6)
    m = F.mfi(H, L, C, V, 14)
    check("mfi all-up = 100", near(m[-1], 100.0, 1e-9), str(m[-1]))
    _, H, L, C, V = tape(40, -0.004, 0.4)
    m = F.mfi(H, L, C, V, 14)
    check("mfi all-down = 0", near(m[-1], 0.0, 1e-9), str(m[-1]))

    # Twiggs must survive a gap that breaks plain CLV. Bar 20 gaps down hard
    # and then closes near ITS OWN high -- CLV calls that strong buying, but
    # relative to the prior close the bar is deeply negative, and the true
    # range Twiggs uses knows that.
    _, H, L, C, V = tape(40, 0.0, 0.5)
    H[20], L[20], C[20] = 90.0, 88.0, 89.9
    t = F.twiggs_money_flow(H, L, C, V, 21)
    clv_b, clv_s = F.clv_split(H[20:21], L[20:21], C[20:21], V[20:21])
    check("clv is fooled by the gap bar", clv_b[0] / V[20] > 0.9,
          f"{clv_b[0] / V[20]:.3f}")
    th, tl = max(H[20], C[19]), min(L[20], C[19])
    tclv = ((C[20] - tl) - (th - C[20])) / (th - tl)
    check("twiggs sees the gap bar as selling", tclv < -0.5, f"{tclv:.3f}")
    check("twiggs produces a value", t[-1] is not None, str(t[-1]))

    f = F.force_index([10, 11, 12, 13] * 6, [100.0] * 24, 13)
    check("force index positive on rising tape", f[-1] is not None and f[-1] > 0,
          str(f[-1]))

    # U/D ratio: 30 bars where up days carry 3x the volume of down days
    closes, vols = [100.0], [0.0]
    for i in range(30):
        up = i % 2 == 0
        closes.append(closes[-1] * (1.01 if up else 0.995))
        vols.append(3_000_000.0 if up else 1_000_000.0)
    # the 25-bar window holds 12 up days and 13 down days, so 12*3M / 13*1M
    ud = F.up_down_volume_ratio(closes, vols, 25)
    check("ud ratio", near(ud, 36 / 13, 1e-9), str(ud))
    # hand-checkable case: up volume 200+300, down volume 100
    check("ud ratio exact", near(F.up_down_volume_ratio([10, 11, 10, 12],
                                                        [0, 200, 100, 300], 3), 5.0),
          str(F.up_down_volume_ratio([10, 11, 10, 12], [0, 200, 100, 300], 3)))
    check("ud ratio short series", F.up_down_volume_ratio([1, 2], [1, 2], 25) is None)
    check("ud ratio no down days", near(F.up_down_volume_ratio([10, 11, 12],
                                                               [0, 100, 100], 2), 99.0))

    acc, dist = F.accum_dist_days(closes, vols, 25)
    check("accumulation days counted", acc > 0 and dist == 0, f"acc={acc} dist={dist}")


def test_helpers():
    check("sma", F.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5])
    e = F.ema([1, 2, 3, 4, 5], 3)
    check("ema seeds with sma", near(e[2], 2.0), str(e))
    check("ema step", near(e[3], 2.0 + (4 - 2.0) * 0.5), str(e))
    check("spearman +1", near(F.spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]), 1.0, 1e-9))
    check("spearman -1", near(F.spearman([1, 2, 3, 4, 5], [10, 8, 6, 4, 2]), -1.0, 1e-9))
    check("spearman short", F.spearman([1, 2], [2, 1]) is None)
    check("stdev", near(F.stdev([2, 4, 4, 4, 5, 5, 7, 9]), 2.138089935299395, 1e-9))
    a = F.atr([2, 3, 4], [1, 2, 3], [1.5, 2.5, 3.5], 2)
    check("atr computes", a[-1] is not None, str(a))
    check("clamp", F.clamp(5) == 1.0 and F.clamp(-5) == -1.0 and F.clamp(0.3) == 0.3)


# ---------------------------------------------------------------------------
# 5. Wyckoff absorption
# ---------------------------------------------------------------------------

def test_absorption():
    # 30 quiet bars, then bars with 6x volume and almost no price movement,
    # sitting at the BOTTOM of the recent range -> supply being absorbed.
    O, H, L, C, V = tape(30, -0.004, 0.5, vol=1_000_000)
    for _ in range(4):
        base = C[-1]
        O.append(base); H.append(base * 1.002); L.append(base * 0.998)
        C.append(base * 1.0001); V.append(6_000_000.0)
    r = F.effort_vs_result(H, L, C, V, lookback=6, vol_win=20)
    check("absorption events found", r["events"] >= 2, str(r))
    check("absorption reads as accumulation", r["dir"] == 1, str(r))

    # same effort/no-result bars, but at the TOP of the range -> distribution
    O, H, L, C, V = tape(30, 0.004, 0.5, vol=1_000_000)
    for _ in range(4):
        base = C[-1]
        O.append(base); H.append(base * 1.002); L.append(base * 0.998)
        C.append(base * 0.9999); V.append(6_000_000.0)
    r = F.effort_vs_result(H, L, C, V, lookback=6, vol_win=20)
    check("absorption at highs reads as distribution", r["dir"] == -1, str(r))

    # an ordinary tape must not fire
    O, H, L, C, V = tape(40, 0.002, 0.5)
    r = F.effort_vs_result(H, L, C, V, lookback=10, vol_win=20)
    check("no false absorption", r["events"] == 0, str(r))
    check("absorption short series safe",
          F.effort_vs_result([1], [1], [1], [1])["events"] == 0)


# ---------------------------------------------------------------------------
# 6. volume profile
# ---------------------------------------------------------------------------

def test_profile():
    # heavy trade concentrated at 100, thin tails -> POC must land on 100
    H, L, C, V = [], [], [], []
    for _ in range(50):
        H.append(100.2); L.append(99.8); C.append(100.0); V.append(1_000_000.0)
    for _ in range(5):
        H.append(105.2); L.append(104.8); C.append(105.0); V.append(50_000.0)
    for _ in range(5):
        H.append(95.2); L.append(94.8); C.append(95.0); V.append(50_000.0)
    b, s = F.clv_split(H, L, C, V)
    p = F.volume_profile(H, L, C, V, b, s, bins=20)
    check("poc at the heavy price", abs(p["poc"] - 100.0) < 0.6, str(p["poc"]))
    check("value area brackets the poc", p["val"] <= p["poc"] <= p["vah"],
          f"{p['val']} {p['poc']} {p['vah']}")
    check("profile conserves volume", near(sum(l["v"] for l in p["levels"]),
                                           sum(V), 1e-3))
    check("profile bin count", len(p["levels"]) == 20)
    # value area must be a proper subset when volume is concentrated
    check("value area is tight", (p["vah"] - p["val"]) < (max(H) - min(L)) * 0.5,
          f"{p['vah'] - p['val']}")
    # signed profile: selling at a level must show up as negative delta there
    Hs = [105.2] * 20; Ls = [104.8] * 20; Cs = [104.8] * 20; Vs = [1e6] * 20
    H2, L2, C2, V2 = H + Hs, L + Ls, C + Cs, V + Vs
    b2, s2 = F.clv_split(H2, L2, C2, V2)
    p2 = F.volume_profile(H2, L2, C2, V2, b2, s2, bins=20)
    top = max(p2["levels"], key=lambda x: x["p"])
    check("supply shelf shows negative delta", top["d"] < 0, str(top))
    check("profile empty series safe", F.volume_profile([], [], [], [])["poc"] is None)


def test_vwap_and_impact():
    v = F.vwap([10, 10], [10, 10], [10, 10], [100, 100])
    check("vwap flat", near(v[-1], 10.0))
    v = F.vwap([10, 20], [10, 20], [10, 20], [100, 300])
    check("vwap volume weighted", near(v[-1], (10 * 100 + 20 * 300) / 400))
    imp = F.amihud_impact([100, 101, 102], [1e6, 1e6, 1e6], 20)
    check("amihud positive", imp is not None and imp > 0, str(imp))
    check("amihud zero-volume safe", F.amihud_impact([100, 101], [0, 0], 20) is None)


def test_rvol_tod():
    # two sessions, the second running at 3x the first at the same clock time
    ts, vols = [], []
    day0 = 1_700_000_000 // 86400 * 86400
    for d in range(2):
        for k in range(20):
            ts.append(day0 + d * 86400 + 14 * 3600 + k * 300)
            vols.append(100_000.0 * (3 if d == 1 else 1))
    r, cum, base = F.rvol_by_time_of_day(ts, vols)
    check("rvol tod ~3x", r is not None and 2.9 < r < 3.1, str(r))
    check("rvol single session safe",
          F.rvol_by_time_of_day(ts[:20], vols[:20])[0] is None)
    check("rvol empty safe", F.rvol_by_time_of_day([], [])[0] is None)


# ---------------------------------------------------------------------------
# 7. composite score -- the acceptance tests
# ---------------------------------------------------------------------------

# The component mapping now lives in flow_lib.analyze_daily, so these tests
# exercise the real code path the scanner uses rather than a copy of it.


def score_of(H, L, C, V):
    r = F.analyze_daily(H, L, C, V)
    return (r["score"], r) if r else (None, None)


def test_score():
    sc, used = F.flow_score({k: 1.0 for k, _, _ in F.SCORE_WEIGHTS})
    check("score all-positive = 100", near(sc, 100.0, 1e-6), str(sc))
    sc, _ = F.flow_score({k: -1.0 for k, _, _ in F.SCORE_WEIGHTS})
    check("score all-negative = -100", near(sc, -100.0, 1e-6), str(sc))
    sc, _ = F.flow_score({})
    check("score no data = 0", near(sc, 0.0), str(sc))
    sc, used = F.flow_score({"cmf": 1.0, "obv": None})
    check("score ignores missing components", near(sc, 100.0) and "obv" not in used,
          f"{sc} {used}")
    sc, _ = F.flow_score({"cmf": 99.0})
    check("score clamps outliers", near(sc, 100.0), str(sc))

    check("label strong acc", F.label_for(80)[1] == "acc2")
    check("label acc", F.label_for(20)[1] == "acc1")
    check("label neutral", F.label_for(0)[1] == "neu")
    check("label dist", F.label_for(-20)[1] == "dis1")
    check("label strong dist", F.label_for(-80)[1] == "dis2")


def test_end_to_end():
    # ACCUMULATION: steady advance, every bar closing in the upper part of its
    # range, up days carrying the heavier volume.
    _, H, L, C, V = tape(120, 0.003, 0.85, vol_jitter=[1.4, 1.4, 0.7, 1.4, 0.7])
    sc, r = score_of(H, L, C, V)
    check("e2e accumulation scores positive", sc > 40, f"score={sc} parts={r['parts']}")
    check("e2e accumulation labelled", r["cls"] in ("acc1", "acc2"), r["label"])

    # DISTRIBUTION: steady decline, closing in the lower part of the range.
    _, H, L, C, V = tape(120, -0.003, 0.15, vol_jitter=[0.7, 1.4, 1.4, 0.7, 1.4])
    sc, r = score_of(H, L, C, V)
    check("e2e distribution scores negative", sc < -40, f"score={sc} parts={r['parts']}")
    check("e2e distribution labelled", r["cls"] in ("dis1", "dis2"), r["label"])

    # THE CASE THE WHOLE TOOL EXISTS FOR: price grinding UP while every bar is
    # sold to its low. A price chart calls this an uptrend; the flow read must
    # call it distribution, and must NOT be outvoted by the close-to-close
    # indicators that merely echo the price trend.
    _, H, L, C, V = tape(120, 0.004, 0.05, vol_jitter=[1.4, 1.4, 0.7, 1.4, 0.7])
    sc, r = score_of(H, L, C, V)
    check("e2e rally-being-sold reads as distribution", sc < -10,
          f"score={sc} parts={r['parts']}")
    check("e2e rally-being-sold flags bearish divergence", r["divergence"] == -1,
          str(r["divergence"]))

    # The mirror image: price sliding while every bar is bought back to its
    # high -- dips absorbed, which is accumulation.
    _, H, L, C, V = tape(120, -0.004, 0.95, vol_jitter=[1.4, 1.4, 0.7, 1.4, 0.7])
    sc, r = score_of(H, L, C, V)
    check("e2e dips-being-bought reads as accumulation", sc > 10,
          f"score={sc} parts={r['parts']}")
    check("e2e dips-being-bought flags bullish divergence", r["divergence"] == 1,
          str(r["divergence"]))

    # A choppy tape with no net flow must sit near neutral rather than
    # inventing a signal -- the failure mode that matters most in practice.
    H, L, C, V = [], [], [], []
    px = 100.0
    for i in range(120):
        up = i % 2 == 0
        nxt = px * (1.008 if up else 0.992)
        H.append(max(px, nxt) * 1.002); L.append(min(px, nxt) * 0.998)
        C.append(nxt); V.append(1_000_000.0)
        px = nxt
    sc, r = score_of(H, L, C, V)
    check("e2e choppy tape is neutral", -25 < sc < 25, f"score={sc} parts={r['parts']}")

    check("analyze_daily rejects short history",
          F.analyze_daily([1] * 10, [1] * 10, [1] * 10, [1] * 10) is None)


def test_analyze_intraday():
    # two sessions of 5-minute bars trending up with closes near the highs
    _, H, L, C, V = tape(160, 0.001, 0.9, vol=200_000)
    t0 = 1_700_000_000
    ts = [t0 + i * 300 for i in range(80)] + [t0 + 86400 + i * 300 for i in range(80)]
    r = F.analyze_intraday(ts, H, L, C, V, adv=16_000_000)
    check("intraday returns a result", r is not None)
    check("intraday net delta positive", r["netDeltaPct"] > 0, str(r["netDeltaPct"]))
    check("intraday vwap below a rising close", r["vwap"] < C[-1],
          f"{r['vwap']} {C[-1]}")
    check("intraday profile complete", len(r["profile"]) == 24)
    check("intraday value area ordered", r["val"] <= r["poc"] <= r["vah"],
          f"{r['val']} {r['poc']} {r['vah']}")
    check("intraday vpin present", r["vpin"] is not None, str(r["vpin"]))
    check("intraday rvol present", r["rvol"] is not None, str(r["rvol"]))
    check("analyze_intraday rejects short history",
          F.analyze_intraday([1] * 5, [1] * 5, [1] * 5, [1] * 5, [1] * 5) is None)


def main():
    for fn in (test_splits, test_agreement, test_cvd_and_divergence, test_vpin,
               test_classics, test_helpers, test_absorption, test_profile,
               test_vwap_and_impact, test_rvol_tod, test_score, test_end_to_end,
               test_analyze_intraday):
        fn()
    print(f"{PASSED} passed, {len(FAILED)} failed")
    for f in FAILED:
        print("  FAIL " + f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
