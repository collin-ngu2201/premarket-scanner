#!/usr/bin/env python3
"""Order-flow math: turning unsigned volume into buying vs selling pressure.

A volume bar is UNSIGNED. It says how much traded, never who was the
aggressor. Recovering the sign properly needs trade-level data with the
prevailing bid/ask (the Lee-Ready test): a print at the ask is a buy, at the
bid a sell. From free OHLCV bars the sign has to be *estimated* -- so this
module implements three independent estimators and treats their agreement as
the confidence measure, rather than pretending any one of them is truth.

  BVC   Bulk Volume Classification (Easley, Lopez de Prado & O'Hara, 2012).
        buy_fraction = CDF(dP / sigma_dP) -- a probabilistic split rather than
        an all-or-nothing one. This is the classifier VPIN is built on and the
        best single estimator available from bar data.
  CLV   Close Location Value (Chaikin). Where in the bar's own range the close
        landed: ((C-L)-(H-C))/(H-L). Bar-internal, so unlike BVC and the tick
        rule it is not fooled by an overnight gap.
  TICK  The classic tick rule: sign(dP) * V, zero-ticks carried forward. Crude
        and binary, kept as an independent third opinion.

Everything downstream (CVD, VPIN, the volume profile's delta) is built on a
chosen estimator, so a caller can swap one in and compare.

Pure stdlib, no numpy -- matches the rest of tools/ so it runs anywhere.
All series functions take plain lists and return lists of the same length,
using None for "not enough history yet".
"""
import math

# ---------------------------------------------------------------------------
# small numeric helpers
# ---------------------------------------------------------------------------

def norm_cdf(z):
    """Standard normal CDF via erf (no scipy)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def clamp(v, lo=-1.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def stdev(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def sma(xs, n):
    """Rolling simple moving average; None until n samples exist."""
    out, run = [], 0.0
    for i, x in enumerate(xs):
        run += x
        if i >= n:
            run -= xs[i - n]
        out.append(run / n if i >= n - 1 else None)
    return out


def ema(xs, n):
    """Rolling EMA seeded with the first n-sample SMA."""
    out = [None] * len(xs)
    if len(xs) < n:
        return out
    k = 2.0 / (n + 1.0)
    prev = sum(xs[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(xs)):
        prev = xs[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def zscore(xs, n):
    """Rolling z-score of the latest value against the prior n samples."""
    out = [None] * len(xs)
    for i in range(len(xs)):
        win = xs[max(0, i - n + 1):i + 1]
        if len(win) < max(5, n // 3):
            continue
        s = stdev(win)
        if not s:
            continue
        out[i] = (xs[i] - (sum(win) / len(win))) / s
    return out


def slope(xs):
    """Least-squares slope per bar of a series (None-tolerant)."""
    pts = [(i, x) for i, x in enumerate(xs) if x is not None]
    n = len(pts)
    if n < 3:
        return None
    sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts); sxy = sum(p[0] * p[1] for p in pts)
    den = n * sxx - sx * sx
    return (n * sxy - sx * sy) / den if den else None


def _ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):                       # average ties
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(a, b):
    """Rank correlation -- robust to the different scales of price and CVD."""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < 5:
        return None
    ra, rb = _ranks([p[0] for p in pairs]), _ranks([p[1] for p in pairs])
    n = len(ra)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = math.sqrt(sum((r - ma) ** 2 for r in ra))
    db = math.sqrt(sum((r - mb) ** 2 for r in rb))
    return num / (da * db) if da and db else None


def true_range(highs, lows, closes):
    out = [highs[0] - lows[0]] if highs else []
    for i in range(1, len(closes)):
        pc = closes[i - 1]
        out.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    return out


def atr(highs, lows, closes, n=14):
    """Wilder's ATR."""
    tr = true_range(highs, lows, closes)
    out = [None] * len(tr)
    if len(tr) < n:
        return out
    prev = sum(tr[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(tr)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# 1. signed volume -- the three estimators
# ---------------------------------------------------------------------------

BVC_WINDOW = 50


def bvc_split(closes, volumes, window=BVC_WINDOW):
    """Bulk Volume Classification -> (buy, sell) volume series.

    buy_t = V_t * Phi(dP_t / sigma_dP), with sigma estimated over a rolling
    window of bar-to-bar changes. A bar that moved up a lot against a quiet
    backdrop is classified as nearly all buying; a bar that barely moved
    splits close to 50/50, which is the honest answer.
    """
    n = len(closes)
    buy, sell = [0.0] * n, [0.0] * n
    diffs = [0.0] + [closes[i] - closes[i - 1] for i in range(1, n)]
    for i in range(n):
        v = volumes[i] or 0.0
        win = diffs[max(1, i - window + 1):i + 1]
        s = stdev(win) if len(win) >= 5 else None
        frac = 0.5 if not s else norm_cdf(diffs[i] / s)
        buy[i] = v * frac
        sell[i] = v * (1.0 - frac)
    return buy, sell


def clv_split(highs, lows, closes, volumes):
    """Chaikin Close Location Value -> (buy, sell).

    CLV = ((C-L) - (H-C)) / (H-L) in [-1, +1]. Closing on the high means
    buyers held the bar to the last print. Because it only looks inside the
    bar it is immune to gaps -- which matters a lot for a pre-market scanner.
    """
    n = len(closes)
    buy, sell = [0.0] * n, [0.0] * n
    for i in range(n):
        rng = highs[i] - lows[i]
        clv = 0.0 if rng <= 0 else ((closes[i] - lows[i]) - (highs[i] - closes[i])) / rng
        v = volumes[i] or 0.0
        buy[i] = v * (1.0 + clv) / 2.0
        sell[i] = v * (1.0 - clv) / 2.0
    return buy, sell


def tick_split(closes, volumes):
    """Tick rule -> (buy, sell). Zero-ticks inherit the previous sign."""
    n = len(closes)
    buy, sell = [0.0] * n, [0.0] * n
    sign = 1
    for i in range(n):
        if i > 0:
            d = closes[i] - closes[i - 1]
            if d > 0:
                sign = 1
            elif d < 0:
                sign = -1
        v = volumes[i] or 0.0
        if sign > 0:
            buy[i] = v
        else:
            sell[i] = v
    return buy, sell


def split_volume(highs, lows, closes, volumes, method="bvc"):
    if method == "clv":
        return clv_split(highs, lows, closes, volumes)
    if method == "tick":
        return tick_split(closes, volumes)
    return bvc_split(closes, volumes)


def method_agreement(highs, lows, closes, volumes, lookback=20):
    """How many of {BVC, CLV, tick} agree on the sign of net flow.

    Returns (agree_count 1..3, direction -1/0/+1). Three-way agreement is the
    high-conviction case; one-vs-two means the estimators are seeing different
    things and the read should be discounted.
    """
    sl = slice(-lookback, None)
    nets = []
    for m in ("bvc", "clv", "tick"):
        b, s = split_volume(highs, lows, closes, volumes, m)
        nets.append(sum(b[sl]) - sum(s[sl]))
    pos = sum(1 for x in nets if x > 0)
    neg = sum(1 for x in nets if x < 0)
    if pos >= neg:
        return pos, (1 if pos > neg else 0)
    return neg, -1


# ---------------------------------------------------------------------------
# 2. cumulative volume delta + divergence
# ---------------------------------------------------------------------------

def cvd(buy, sell):
    """Cumulative volume delta -- the running net of buying over selling."""
    out, run = [], 0.0
    for b, s in zip(buy, sell):
        run += b - s
        out.append(run)
    return out


# Thresholds for calling a price/CVD disagreement a divergence: price must have
# actually gone somewhere, and CVD must have net-travelled a fifth of its own
# in-window range against it. Mirrored in indicators/flow_delta.pine.
PX_MOVE_THR = 0.01
CVD_MOVE_THR = 0.20


def cvd_divergence(closes, cvd_series, lookback=20):
    """Compare where price went with where the money went.

    The single most useful read in this whole module. Price and CVD normally
    travel together; when they part company, one of them is lying:

      price up  + CVD down  -> rallies are being SOLD into (distribution)
      price down + CVD up   -> dips are being BOUGHT (absorption/accumulation)

    Returns dict with the normalised move of each, the rank correlation, and a
    -1/0/+1 flag for the two divergence cases.
    """
    if len(closes) < lookback + 1:
        return {"kind": 0, "corr": None, "pxChg": None, "cvdChg": None}
    px = closes[-lookback:]
    cd = cvd_series[-lookback:]
    px_chg = (px[-1] - px[0]) / px[0] if px[0] else 0.0
    # Normalise the CVD move by how far it ranged WITHIN the window, not by its
    # absolute level. CVD is a running total, so its level depends on how much
    # history happened to precede the window; dividing by that would make the
    # test steadily less sensitive the longer the series is -- the same pattern
    # would flag on a 120-bar series and go silent on a 200-bar one.
    span = max(cd) - min(cd)
    cvd_chg = (cd[-1] - cd[0]) / span if span > 0 else 0.0
    corr = spearman(px, cd)
    kind = 0
    if px_chg > PX_MOVE_THR and cvd_chg < -CVD_MOVE_THR:
        kind = -1                                   # bearish: sellers escaping
    elif px_chg < -PX_MOVE_THR and cvd_chg > CVD_MOVE_THR:
        kind = 1                                    # bullish: buyers injecting
    return {"kind": kind, "corr": corr, "pxChg": px_chg, "cvdChg": cvd_chg}


# ---------------------------------------------------------------------------
# 3. VPIN -- order-flow toxicity (Easley, Lopez de Prado & O'Hara)
# ---------------------------------------------------------------------------

def vpin(buy, sell, volumes, bucket_volume, n_buckets=50):
    """Volume-Synchronised Probability of Informed Trading.

    Slices the tape into equal-VOLUME buckets (not equal time -- activity, not
    the clock, is what matters) and measures how one-sided each bucket was:

        VPIN = mean over the last n buckets of |Vbuy - Vsell| / Vbucket

    High VPIN means flow is lopsided -- someone is working a large one-way
    order and liquidity providers are on the wrong side of it. It spiked
    ahead of the 2010 Flash Crash, which is what made it famous. VPIN itself
    is unsigned, so `signed` carries the direction: positive = the lopsided
    side is buying, negative = selling.
    """
    if not bucket_volume or bucket_volume <= 0:
        return {"vpin": None, "signed": None, "buckets": 0}
    imb, sgn = [], []
    cap = bucket_volume
    ab = as_ = 0.0
    for i in range(len(volumes)):
        v = volumes[i] or 0.0
        b, s = buy[i], sell[i]
        while v > 0:
            take = min(v, cap)
            frac = take / v if v else 0.0
            ab += b * frac
            as_ += s * frac
            b -= b * frac
            s -= s * frac
            v -= take
            cap -= take
            if cap <= 1e-9:                         # bucket full -> record it
                tot = ab + as_
                if tot > 0:
                    imb.append(abs(ab - as_) / tot)
                    sgn.append((ab - as_) / tot)
                ab = as_ = 0.0
                cap = bucket_volume
    if not imb:
        return {"vpin": None, "signed": None, "buckets": 0}
    tail_i, tail_s = imb[-n_buckets:], sgn[-n_buckets:]
    return {"vpin": sum(tail_i) / len(tail_i),
            "signed": sum(tail_s) / len(tail_s),
            "buckets": len(imb)}


# ---------------------------------------------------------------------------
# 4. classical volume/money-flow indicators (the confirmation set)
# ---------------------------------------------------------------------------

def obv(closes, volumes):
    """On-Balance Volume -- Granville's cumulative up/down volume."""
    out, run = [], 0.0
    for i in range(len(closes)):
        if i:
            if closes[i] > closes[i - 1]:
                run += volumes[i]
            elif closes[i] < closes[i - 1]:
                run -= volumes[i]
        out.append(run)
    return out


def ad_line(highs, lows, closes, volumes):
    """Chaikin Accumulation/Distribution line = cumulative CLV * volume."""
    b, s = clv_split(highs, lows, closes, volumes)
    return cvd(b, s)


def cmf(highs, lows, closes, volumes, n=21):
    """Chaikin Money Flow: n-period sum of CLV*V over sum of V. ~[-1, +1]."""
    out = [None] * len(closes)
    mfv = []
    for i in range(len(closes)):
        rng = highs[i] - lows[i]
        clv = 0.0 if rng <= 0 else ((closes[i] - lows[i]) - (highs[i] - closes[i])) / rng
        mfv.append(clv * (volumes[i] or 0.0))
    for i in range(n - 1, len(closes)):
        vs = sum(volumes[i - n + 1:i + 1])
        if vs > 0:
            out[i] = sum(mfv[i - n + 1:i + 1]) / vs
    return out


def twiggs_money_flow(highs, lows, closes, volumes, n=21):
    """Twiggs Money Flow -- CMF rebuilt on TRUE range.

    Chaikin's CLV uses the bar's own high/low, so an overnight gap makes a bar
    look like it closed mid-range when in reality it never traded near the
    prior close. Twiggs substitutes the gap-aware true high/low, which matters
    for exactly the gappy pre-market names this repo screens.
    """
    n_ = len(closes)
    out = [None] * n_
    if n_ < 2:
        return out
    num, den = [], []
    for i in range(n_):
        if i == 0:
            th, tl = highs[i], lows[i]
        else:
            th, tl = max(highs[i], closes[i - 1]), min(lows[i], closes[i - 1])
        rng = th - tl
        clv = 0.0 if rng <= 0 else ((closes[i] - tl) - (th - closes[i])) / rng
        v = volumes[i] or 0.0
        num.append(clv * v)
        den.append(v)
    en, ed = ema(num, n), ema(den, n)
    for i in range(n_):
        if en[i] is not None and ed[i]:
            out[i] = en[i] / ed[i]
    return out


def mfi(highs, lows, closes, volumes, n=14):
    """Money Flow Index -- RSI computed on dollar flow instead of price. 0-100."""
    out = [None] * len(closes)
    tp = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(closes))]
    pos, neg = [0.0] * len(closes), [0.0] * len(closes)
    for i in range(1, len(closes)):
        raw = tp[i] * (volumes[i] or 0.0)
        if tp[i] > tp[i - 1]:
            pos[i] = raw
        elif tp[i] < tp[i - 1]:
            neg[i] = raw
    for i in range(n, len(closes)):
        p = sum(pos[i - n + 1:i + 1]); q = sum(neg[i - n + 1:i + 1])
        out[i] = 100.0 if q == 0 else 100.0 - 100.0 / (1.0 + p / q)
    return out


def force_index(closes, volumes, n=13):
    """Elder's Force Index: (C - C_prev) * V, EMA-smoothed.

    Combines direction, extent and volume in one number -- the only one of the
    classics that scales the move by how much was actually traded to get it.
    """
    raw = [0.0] + [(closes[i] - closes[i - 1]) * (volumes[i] or 0.0)
                   for i in range(1, len(closes))]
    return ema(raw, n)


def vpt(closes, volumes):
    """Volume Price Trend -- cumulative V * pct-change."""
    out, run = [0.0], 0.0
    for i in range(1, len(closes)):
        if closes[i - 1]:
            run += (volumes[i] or 0.0) * (closes[i] - closes[i - 1]) / closes[i - 1]
        out.append(run)
    return out


def ease_of_movement(highs, lows, volumes, n=14, scale=1e8):
    """Arms' Ease of Movement -- how far price travels per unit of volume.

    Large positive values mean price is rising on light volume (little
    resistance); near zero on heavy volume means someone is soaking it up.
    """
    raw = [0.0]
    for i in range(1, len(highs)):
        mid = (highs[i] + lows[i]) / 2.0 - (highs[i - 1] + lows[i - 1]) / 2.0
        rng = highs[i] - lows[i]
        v = volumes[i] or 0.0
        raw.append(0.0 if v <= 0 or rng <= 0 else mid / (v / scale / rng))
    return sma(raw, n)


def up_down_volume_ratio(closes, volumes, n=25):
    """O'Neil's U/D ratio: volume on up days over volume on down days.

    Above ~1.25 over 25 sessions is the classic screen for a name under
    institutional accumulation; below ~0.80 says the funds are leaving.
    """
    if len(closes) < n + 1:
        return None
    up = dn = 0.0
    for i in range(len(closes) - n, len(closes)):
        v = volumes[i] or 0.0
        if closes[i] > closes[i - 1]:
            up += v
        elif closes[i] < closes[i - 1]:
            dn += v
    return up / dn if dn > 0 else (99.0 if up > 0 else None)


def accum_dist_days(closes, volumes, n=25, thr=0.002):
    """IBD-style institutional footprint count over the last n sessions.

    A distribution day = the close falls more than `thr` on HIGHER volume than
    the prior session: size going out. An accumulation day is the mirror. Five
    or more distribution days in a 25-day window is the traditional warning
    that institutions are stepping off.
    """
    acc = dist = 0
    for i in range(max(1, len(closes) - n), len(closes)):
        if (volumes[i] or 0) <= (volumes[i - 1] or 0):
            continue
        if not closes[i - 1]:
            continue
        chg = (closes[i] - closes[i - 1]) / closes[i - 1]
        if chg < -thr:
            dist += 1
        elif chg > thr:
            acc += 1
    return acc, dist


# ---------------------------------------------------------------------------
# 5. Wyckoff: effort vs result (absorption)
# ---------------------------------------------------------------------------

def effort_vs_result(highs, lows, closes, volumes, lookback=10, vol_win=20):
    """Wyckoff's effort-vs-result test.

    Volume is the EFFORT, the price move is the RESULT. Heavy volume that
    produces almost no net movement means someone large is absorbing
    everything thrown at them -- the footprint of a real position being built
    or unloaded, and it shows up BEFORE the move does.

    Direction comes from where in the recent range it happened: absorption at
    the lows is accumulation, at the highs it is distribution.
    """
    n = len(closes)
    if n < vol_win + 2:
        return {"events": 0, "dir": 0, "lastZ": None}
    vz = zscore(volumes, vol_win)
    a = atr(highs, lows, closes, 14)
    events, direction = 0, 0
    for i in range(max(1, n - lookback), n):
        if vz[i] is None or not a[i] or not closes[i - 1]:
            continue
        move = abs(closes[i] - closes[i - 1]) / a[i]        # result, in ATRs
        if vz[i] >= 1.5 and move <= 0.5:                    # effort >> result
            events += 1
            win_h = max(highs[max(0, i - 20):i + 1])
            win_l = min(lows[max(0, i - 20):i + 1])
            rng = win_h - win_l
            pos = 0.5 if rng <= 0 else (closes[i] - win_l) / rng
            direction += 1 if pos < 0.4 else (-1 if pos > 0.6 else 0)
    return {"events": events,
            "dir": 1 if direction > 0 else (-1 if direction < 0 else 0),
            "lastZ": vz[-1]}


def amihud_impact(closes, volumes, n=20):
    """Amihud illiquidity: |return| per $1M traded.

    The price-impact cost of size. A move on a LOW impact reading was paid for
    with real volume; the same move on a HIGH reading means the tape was thin
    and the move is far easier to reverse.
    """
    vals = []
    for i in range(max(1, len(closes) - n), len(closes)):
        dv = closes[i] * (volumes[i] or 0.0)
        if dv <= 0 or not closes[i - 1]:
            continue
        vals.append(abs(closes[i] - closes[i - 1]) / closes[i - 1] / (dv / 1e6))
    return mean(vals)


# ---------------------------------------------------------------------------
# 6. price-level microstructure: VWAP + signed volume profile
# ---------------------------------------------------------------------------

def vwap(highs, lows, closes, volumes):
    """Running VWAP on typical price -- the average participant's cost basis."""
    out, pv, vv = [], 0.0, 0.0
    for i in range(len(closes)):
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        v = volumes[i] or 0.0
        pv += tp * v
        vv += v
        out.append(pv / vv if vv > 0 else closes[i])
    return out


def volume_profile(highs, lows, closes, volumes, buy=None, sell=None,
                   bins=24, value_area=0.70):
    """Volume-at-price with a SIGNED delta per level -- a bar-data footprint.

    Each bar's volume is spread evenly across the price bins it spans, and the
    same is done for its estimated buy and sell halves. That gives, for every
    price level: how much traded there (where the real business was done) and
    who won there.

      POC  point of control -- the most-traded price, the tape's centre of
           gravity and the level price keeps returning to.
      VAH/VAL  the band holding `value_area` of the volume. Price leaving it
           on expanding volume is the definition of a breakout worth trusting.

    Levels with heavy volume and a strongly negative delta are supply shelves:
    that is where the escaping happened, and where a rally is likely to stall.
    """
    n = len(closes)
    if n == 0:
        return {"levels": [], "poc": None, "vah": None, "val": None}
    lo, hi = min(lows), max(highs)
    if hi <= lo:
        hi = lo + max(0.01, lo * 0.001)
    w = (hi - lo) / bins
    tot = [0.0] * bins
    dlt = [0.0] * bins
    for i in range(n):
        v = volumes[i] or 0.0
        if v <= 0:
            continue
        d = (buy[i] - sell[i]) if (buy is not None and sell is not None) else 0.0
        b0 = min(bins - 1, max(0, int((lows[i] - lo) / w)))
        b1 = min(bins - 1, max(0, int((highs[i] - lo) / w)))
        span = b1 - b0 + 1
        for b in range(b0, b1 + 1):
            tot[b] += v / span
            dlt[b] += d / span
    grand = sum(tot)
    poc = max(range(bins), key=lambda b: tot[b])
    # grow the value area outward from the POC, always taking the fatter side
    lo_i = hi_i = poc
    acc = tot[poc]
    while acc < grand * value_area and (lo_i > 0 or hi_i < bins - 1):
        down = tot[lo_i - 1] if lo_i > 0 else -1
        up = tot[hi_i + 1] if hi_i < bins - 1 else -1
        if up >= down:
            hi_i += 1; acc += tot[hi_i]
        else:
            lo_i -= 1; acc += tot[lo_i]
    ctr = lambda b: lo + w * (b + 0.5)
    return {
        "levels": [{"p": round(ctr(b), 4), "v": tot[b], "d": dlt[b]} for b in range(bins)],
        "poc": round(ctr(poc), 4),
        "vah": round(ctr(hi_i) + w / 2, 4),
        "val": round(ctr(lo_i) - w / 2, 4),
        "total": grand,
    }


def rvol_by_time_of_day(timestamps, volumes, tz_offset=0, prior_days=5):
    """Relative volume against the SAME time-of-day on earlier sessions.

    A flat daily RVOL is misleading intraday: 09:35 is always busy and 12:30
    never is. Comparing a session only against the same clock window on prior
    days is what makes an intraday volume spike actually mean something.

    Returns (rvol, cumulative_volume_today, baseline).
    """
    if not timestamps:
        return None, None, None
    by_day = {}
    for t, v in zip(timestamps, volumes):
        lt = t + tz_offset
        day, slot = lt // 86400, (lt % 86400) // 300      # 5-minute slots
        by_day.setdefault(day, {})
        by_day[day][slot] = by_day[day].get(slot, 0.0) + (v or 0.0)
    days = sorted(by_day)
    if len(days) < 2:
        return None, None, None
    today = by_day[days[-1]]
    if not today:
        return None, None, None
    last_slot = max(today)
    cum_today = sum(today.values())
    base = [sum(vv for sl, vv in by_day[d].items() if sl <= last_slot)
            for d in days[-1 - prior_days:-1]]
    b = mean([x for x in base if x > 0])
    return (cum_today / b if b else None), cum_today, b


# ---------------------------------------------------------------------------
# 7. composite flow score
# ---------------------------------------------------------------------------

# Component weights for the composite. Deliberately lopsided: the WITHIN-BAR
# family (CLV/CMF/Twiggs and the divergence test) carries most of the weight
# because it is the part that is independent of price. The close-to-close
# family (OBV, MFI, Force) is largely a volume-weighted echo of the price
# trend -- useful as confirmation, but if it were weighted equally then every
# uptrend would read as "accumulation" and the tool would say nothing a price
# chart doesn't already.
SCORE_WEIGHTS = [
    ("netDelta", 1.20, "Net signed volume"),
    ("diverge", 1.00, "Price vs CVD divergence"),
    ("cmf", 1.00, "Chaikin Money Flow"),
    ("twiggs", 0.90, "Twiggs Money Flow (gap-aware)"),
    ("ud", 0.70, "Up/Down volume ratio"),
    ("accdist", 0.70, "Accum / distrib days"),
    ("vpin", 0.60, "VPIN direction"),
    ("obv", 0.40, "OBV trend"),
    ("mfi", 0.40, "Money Flow Index"),
    ("force", 0.30, "Force Index"),
]

SCORE_LABELS = {k: lbl for k, _, lbl in SCORE_WEIGHTS}


def flow_score(parts):
    """Blend the normalised components into one -100..+100 reading.

    Positive = money going IN (accumulation), negative = money coming OUT
    (distribution). Components are each mapped to roughly -1..+1 first, so no
    single indicator can run away with the score, and missing ones drop out of
    both the numerator and the denominator instead of counting as zero.
    """
    num = den = 0.0
    used = {}
    for key, w, _ in SCORE_WEIGHTS:
        v = parts.get(key)
        if v is None:
            continue
        v = clamp(v)
        used[key] = round(v, 3)
        num += w * v
        den += w
    if den == 0:
        return 0.0, used
    return round(100.0 * num / den, 1), used


def label_for(score):
    if score >= 45:
        return "Strong accumulation", "acc2"
    if score >= 15:
        return "Accumulation", "acc1"
    if score > -15:
        return "Neutral / mixed", "neu"
    if score > -45:
        return "Distribution", "dis1"
    return "Strong distribution", "dis2"


# ---------------------------------------------------------------------------
# 8. the two top-level entry points
# ---------------------------------------------------------------------------
#
# Which signed-volume estimator is right depends on the bar size, and getting
# this backwards is the single easiest way to build a misleading tool:
#
#   DAILY bars  -> CLV. A daily bar's close-to-close change is just the price
#                  trend, so BVC's CVD can barely diverge from price and the
#                  divergence test goes blind. Where the close sits inside the
#                  day's range is genuinely new information: a name that gaps
#                  up and then gets sold all session is distribution, and only
#                  the within-bar view sees it.
#   INTRADAY    -> BVC. At 5-minute resolution the bar-to-bar change IS an
#                  aggression proxy, so the probabilistic split is meaningful
#                  and CVD can legitimately part company with price mid-session.

DAILY_METHOD = "clv"
INTRADAY_METHOD = "bvc"


def analyze_daily(highs, lows, closes, volumes, window=20):
    """Multi-week accumulation/distribution read from daily bars.

    Answers "have institutions been building or unloading this name over the
    last month" -- the positional question. Returns the raw indicator values,
    the normalised score components, and the composite.
    """
    n = len(closes)
    if n < 40:
        return None
    buy, sell = split_volume(highs, lows, closes, volumes, DAILY_METHOD)
    cvd_s = cvd(buy, sell)

    vol_w = sum(volumes[-window:]) or 1.0
    net = sum(buy[-window:]) - sum(sell[-window:])
    c_mf = cmf(highs, lows, closes, volumes, 21)[-1]
    t_mf = twiggs_money_flow(highs, lows, closes, volumes, 21)[-1]
    obv_s = obv(closes, volumes)
    obv_sl = slope(obv_s[-window:])
    ud = up_down_volume_ratio(closes, volumes, 25)
    m_fi = mfi(highs, lows, closes, volumes, 14)[-1]
    f_i = force_index(closes, volumes, 13)[-1]
    acc, dist = accum_dist_days(closes, volumes, 25)
    div = cvd_divergence(closes, cvd_s, window)
    absorb = effort_vs_result(highs, lows, closes, volumes, 10, 20)
    agree_n, agree_d = method_agreement(highs, lows, closes, volumes, window)
    adv = mean(volumes[-20:]) or 0.0
    vp = vpin(buy, sell, volumes, adv / 2.0, 20) if adv > 0 else {"vpin": None, "signed": None}
    impact = amihud_impact(closes, volumes, 20)
    dollar_vol = mean([closes[i] * volumes[i] for i in range(max(0, n - 20), n)])

    avg_bar_vol = vol_w / window
    parts = {
        # net signed volume as a share of everything that traded: "of all the
        # shares that changed hands this month, what net fraction was bought"
        "netDelta": net / vol_w,
        # a divergence is a real signal; mere agreement between price and flow
        # is not independent evidence, so it contributes nothing
        "diverge": div["kind"] * 0.9 if div["kind"] else 0.0,
        "cmf": c_mf * 3 if c_mf is not None else None,
        "twiggs": t_mf * 5 if t_mf is not None else None,
        # log2 so 2x reads as +1 and 0.5x as -1; floored so a name with no
        # up-volume at all still registers as maximally negative
        "ud": math.log(max(ud, 0.05)) / math.log(2) if ud is not None else None,
        "accdist": (acc - dist) / 5.0,
        "vpin": (vp["signed"] or 0.0) * 2 if vp.get("signed") is not None else None,
        "obv": obv_sl / avg_bar_vol if (obv_sl is not None and avg_bar_vol) else None,
        "mfi": (m_fi - 50) / 50 if m_fi is not None else None,
        "force": math.tanh(f_i / (avg_bar_vol * closes[-1] * 0.01))
                 if (f_i and avg_bar_vol and closes[-1]) else None,
    }
    score, used = flow_score(parts)
    label, cls = label_for(score)

    return {
        "score": score, "label": label, "cls": cls, "parts": used,
        "netDeltaPct": round(100.0 * net / vol_w, 1),
        "netDeltaShares": round(net),
        "netDeltaUsd": round(net * closes[-1]),
        "cmf": round(c_mf, 4) if c_mf is not None else None,
        "twiggs": round(t_mf, 4) if t_mf is not None else None,
        "mfi": round(m_fi, 1) if m_fi is not None else None,
        "ud": round(ud, 2) if ud else None,
        "accDays": acc, "distDays": dist,
        "divergence": div["kind"],
        "divCorr": round(div["corr"], 3) if div["corr"] is not None else None,
        "absorption": absorb["events"], "absorbDir": absorb["dir"],
        "agree": agree_n, "agreeDir": agree_d,
        "vpin": round(vp["vpin"], 3) if vp.get("vpin") is not None else None,
        "vpinSigned": round(vp["signed"], 3) if vp.get("signed") is not None else None,
        "impact": round(impact, 4) if impact is not None else None,
        "dollarVol": round(dollar_vol) if dollar_vol else None,
        "cvd": [round(x) for x in cvd_s[-120:]],
        "closes": [round(x, 2) for x in closes[-120:]],
    }


def analyze_intraday(timestamps, highs, lows, closes, volumes, adv=None, bins=24):
    """Session-level order-flow read from intraday bars.

    Answers "who is winning the tape right now" -- the tactical question.
    Volume profile, VWAP, intraday CVD and VPIN all come from here.
    """
    n = len(closes)
    if n < 30:
        return None
    buy, sell = split_volume(highs, lows, closes, volumes, INTRADAY_METHOD)
    cvd_s = cvd(buy, sell)
    tot = sum(volumes) or 1.0
    net = sum(buy) - sum(sell)

    prof = volume_profile(highs, lows, closes, volumes, buy, sell, bins)
    vw = vwap(highs, lows, closes, volumes)
    # VPIN's standard bucket is 1/50th of a day's volume
    bucket = (adv or (tot / max(1, len(set(t // 86400 for t in timestamps))))) / 50.0
    vp = vpin(buy, sell, volumes, bucket, 50)
    div = cvd_divergence(closes, cvd_s, min(60, n - 1))
    rv, cum, base = rvol_by_time_of_day(timestamps, volumes)

    last = closes[-1]
    return {
        "netDeltaPct": round(100.0 * net / tot, 1),
        "netDeltaShares": round(net),
        "vwap": round(vw[-1], 2),
        "vsVwapPct": round(100.0 * (last - vw[-1]) / vw[-1], 2) if vw[-1] else None,
        "poc": prof["poc"], "vah": prof["vah"], "val": prof["val"],
        "inValueArea": bool(prof["val"] is not None and prof["val"] <= last <= prof["vah"]),
        "profile": [{"p": l["p"], "v": round(l["v"]), "d": round(l["d"])}
                    for l in prof["levels"]],
        "vpin": round(vp["vpin"], 3) if vp.get("vpin") is not None else None,
        "vpinSigned": round(vp["signed"], 3) if vp.get("signed") is not None else None,
        "divergence": div["kind"],
        "rvol": round(rv, 2) if rv else None,
        "bars": n,
        "cvd": [round(x) for x in cvd_s],
        "closes": [round(x, 2) for x in closes],
        "ts": timestamps,
    }
