// Intraday/daily OHLC proxy (Vercel function).
// Yahoo's v8 chart endpoint is keyless and crumb-free (unlike /v7/quote).
//
//   GET /api/chart?symbol=AAPL&interval=5m&range=1d
//
// Returns compact candles [[t,o,h,l,c], ...] plus prevClose and the regular
// session window (so the client can shade pre/post-market).

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36";

const RANGES = new Set(["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y"]);
const INTERVALS = new Set(["1m", "2m", "5m", "15m", "30m", "60m", "1d", "1wk"]);

const round = (v) => (typeof v === "number" && isFinite(v) ? Math.round(v * 100) / 100 : null);

function send(res, status, obj, cache = "no-store") {
  res.setHeader("content-type", "application/json");
  res.setHeader("cache-control", cache);
  res.status(status).send(JSON.stringify(obj));
}

export default async function handler(req, res) {
  const symbol = String(req.query?.symbol || "").trim().toUpperCase();
  if (!/^[A-Z][A-Z.\-]{0,9}$/.test(symbol)) return send(res, 400, { error: "bad symbol" });
  const range = RANGES.has(req.query?.range) ? req.query.range : "1d";
  const interval = INTERVALS.has(req.query?.interval) ? req.query.interval : "5m";

  const url =
    `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}` +
    `?range=${range}&interval=${interval}&includePrePost=true`;

  try {
    const r = await fetch(url, { headers: { "User-Agent": UA } });
    if (!r.ok) return send(res, 502, { error: "upstream " + r.status });
    const d = await r.json();
    const result = d?.chart?.result?.[0];
    if (!result) return send(res, 502, { error: "no data" });

    const meta = result.meta || {};
    const ts = result.timestamp || [];
    const q = result.indicators?.quote?.[0] || {};
    const candles = [];
    for (let i = 0; i < ts.length; i++) {
      const o = q.open?.[i], h = q.high?.[i], l = q.low?.[i], c = q.close?.[i];
      if (o == null || h == null || l == null || c == null) continue;
      candles.push([ts[i], round(o), round(h), round(l), round(c)]);
    }
    const cp = meta.currentTradingPeriod || {};
    return send(
      res, 200,
      {
        symbol,
        interval,
        prevClose: round(meta.chartPreviousClose),
        regStart: cp.regular?.start ?? null,
        regEnd: cp.regular?.end ?? null,
        candles,
      },
      "public, max-age=45"
    );
  } catch (e) {
    return send(res, 502, { error: String(e.message || e) });
  }
}
