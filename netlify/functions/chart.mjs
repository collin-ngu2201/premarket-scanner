// Intraday OHLC proxy for the divergence dashboard's mini candlesticks.
// Yahoo's v8 chart endpoint is keyless and crumb-free (unlike /v7/quote).
//
//   GET /api/chart?symbol=AAPL&interval=5m&range=1d
//
// Returns compact candles [[t,o,h,l,c], ...] plus prevClose and the regular
// session window (so the client can shade pre/post-market).

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36";

const RANGES = new Set(["1d", "5d"]);
const INTERVALS = new Set(["1m", "2m", "5m", "15m", "30m", "60m"]);

const round = (v) => (typeof v === "number" && isFinite(v) ? Math.round(v * 100) / 100 : null);

function json(obj, status = 200, cache = "no-store") {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", "cache-control": cache },
  });
}

export default async (req) => {
  const u = new URL(req.url);
  const symbol = (u.searchParams.get("symbol") || "").trim().toUpperCase();
  if (!/^[A-Z][A-Z.\-]{0,9}$/.test(symbol)) return json({ error: "bad symbol" }, 400);
  const range = RANGES.has(u.searchParams.get("range")) ? u.searchParams.get("range") : "1d";
  const interval = INTERVALS.has(u.searchParams.get("interval")) ? u.searchParams.get("interval") : "5m";

  const url =
    `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}` +
    `?range=${range}&interval=${interval}&includePrePost=true`;

  try {
    const r = await fetch(url, { headers: { "User-Agent": UA } });
    if (!r.ok) return json({ error: "upstream " + r.status }, 502);
    const d = await r.json();
    const res = d?.chart?.result?.[0];
    if (!res) return json({ error: "no data" }, 502);

    const meta = res.meta || {};
    const ts = res.timestamp || [];
    const q = res.indicators?.quote?.[0] || {};
    const candles = [];
    for (let i = 0; i < ts.length; i++) {
      const o = q.open?.[i], h = q.high?.[i], l = q.low?.[i], c = q.close?.[i];
      if (o == null || h == null || l == null || c == null) continue;
      candles.push([ts[i], round(o), round(h), round(l), round(c)]);
    }
    const cp = meta.currentTradingPeriod || {};
    return json(
      {
        symbol,
        interval,
        prevClose: round(meta.chartPreviousClose),
        regStart: cp.regular?.start ?? null,
        regEnd: cp.regular?.end ?? null,
        candles,
      },
      200,
      "public, max-age=45"
    );
  } catch (e) {
    return json({ error: String(e.message || e) }, 502);
  }
};

export const config = { path: "/api/chart" };
