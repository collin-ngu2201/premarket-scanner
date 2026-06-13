// Batched market-quote proxy for the pre-market scanner.
//
// Free provider  = Yahoo Finance (keyless; handles the cookie + crumb dance and
//                  CORS that a browser can't do directly).
// Upgrade path   = ?provider=polygon reads POLYGON_KEY (stub below). The output
//                  shape is normalized, so the front-end never changes when you
//                  swap providers.
//
// Accepts:
//   GET  /api/quotes?symbols=AAPL,MSFT,...      (handy for quick tests)
//   POST /api/quotes   body: {"symbols":[...]}  (used for the full universe)
//   optional &provider=yahoo|polygon  (default: yahoo)

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36";

const YAHOO_FIELDS = [
  "symbol", "shortName", "longName", "currency", "marketState", "fullExchangeName",
  "regularMarketPrice", "regularMarketChangePercent", "regularMarketVolume",
  "regularMarketPreviousClose",
  "preMarketPrice", "preMarketChangePercent",
  "postMarketPrice", "postMarketChangePercent",
  "marketCap", "averageDailyVolume3Month", "averageDailyVolume10Day",
].join(",");

const BATCH = 100;          // symbols per Yahoo call (well under its cap)
const AUTH_TTL = 25 * 60e3; // re-auth every 25 min

// Cached across warm invocations so we don't re-auth on every request.
let yahooAuth = { cookie: null, crumb: null, ts: 0 };

function readSetCookies(resp) {
  if (typeof resp.headers.getSetCookie === "function") return resp.headers.getSetCookie();
  const h = resp.headers.get("set-cookie");
  return h ? [h] : [];
}

async function getYahooAuth(force = false) {
  if (!force && yahooAuth.crumb && Date.now() - yahooAuth.ts < AUTH_TTL) return yahooAuth;

  // fc.yahoo.com 404s but sets the A1/A3 consent cookies the crumb endpoint needs;
  // finance.yahoo.com adds the rest. Merge cookies from both (last value wins per name).
  const jar = new Map();
  for (const url of ["https://fc.yahoo.com", "https://finance.yahoo.com/"]) {
    try {
      const r = await fetch(url, { headers: { "User-Agent": UA, Accept: "text/html" } });
      for (const c of readSetCookies(r)) {
        const [pair] = c.split(";");
        const eq = pair.indexOf("=");
        if (eq > 0) jar.set(pair.slice(0, eq).trim(), pair.slice(eq + 1).trim());
      }
    } catch { /* ignore — fc.yahoo.com may throw on some runtimes */ }
  }
  const cookie = [...jar].map(([k, v]) => `${k}=${v}`).join("; ");

  const cr = await fetch("https://query1.finance.yahoo.com/v1/test/getcrumb", {
    headers: { "User-Agent": UA, Cookie: cookie, Accept: "text/plain" },
  });
  const crumb = (await cr.text()).trim();
  if (!crumb || crumb.includes("<") || crumb.length > 40)
    throw new Error("Yahoo crumb fetch failed");

  yahooAuth = { cookie, crumb, ts: Date.now() };
  return yahooAuth;
}

function chunk(arr, n) {
  const out = [];
  for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n));
  return out;
}

function normalizeYahoo(q) {
  return {
    symbol: q.symbol,
    name: q.shortName || q.longName || q.symbol,
    currency: q.currency || "USD",
    exchange: q.fullExchangeName || null,
    marketState: q.marketState || "UNKNOWN", // PRE | REGULAR | POST | POSTPOST | CLOSED
    price: num(q.regularMarketPrice),
    prevClose: num(q.regularMarketPreviousClose),
    regChangePct: num(q.regularMarketChangePercent),
    regVolume: num(q.regularMarketVolume),
    avgVol3m: num(q.averageDailyVolume3Month),
    avgVol10d: num(q.averageDailyVolume10Day),
    marketCap: num(q.marketCap),
    preMarketPrice: num(q.preMarketPrice),
    preMarketChangePct: num(q.preMarketChangePercent),
    postMarketPrice: num(q.postMarketPrice),
    postMarketChangePct: num(q.postMarketChangePercent),
  };
}

const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);

async function yahooQuotes(symbols) {
  const out = [];
  for (const group of chunk(symbols, BATCH)) {
    let auth = await getYahooAuth();
    const build = (a) =>
      `https://query1.finance.yahoo.com/v7/finance/quote` +
      `?symbols=${encodeURIComponent(group.join(","))}` +
      `&fields=${YAHOO_FIELDS}&crumb=${encodeURIComponent(a.crumb)}`;

    let r = await fetch(build(auth), { headers: { "User-Agent": UA, Cookie: auth.cookie } });
    if (r.status === 401 || r.status === 403) {
      auth = await getYahooAuth(true); // crumb likely expired — re-auth once
      r = await fetch(build(auth), { headers: { "User-Agent": UA, Cookie: auth.cookie } });
    }
    if (!r.ok) throw new Error(`Yahoo upstream ${r.status}`);
    const data = await r.json();
    const res = data?.quoteResponse?.result || [];
    for (const q of res) out.push(normalizeYahoo(q));
  }
  return out;
}

// ---- Polygon stub (the upgrade path) ------------------------------------
// Wire this up when you move to a paid full-universe feed. Polygon's snapshot
// endpoint returns the whole market in one call, including pre-market.
async function polygonQuotes(/* symbols */) {
  const key = process.env.POLYGON_KEY;
  if (!key) throw new Error("POLYGON_KEY not configured");
  throw new Error("Polygon provider not implemented yet — set provider=yahoo");
}

export default async (req) => {
  const url = new URL(req.url);
  const provider = url.searchParams.get("provider") || "yahoo";

  let symbols = [];
  if (req.method === "POST") {
    try {
      const body = await req.json();
      symbols = Array.isArray(body?.symbols) ? body.symbols : [];
    } catch {
      return json({ error: "bad JSON body" }, 400);
    }
  } else {
    symbols = (url.searchParams.get("symbols") || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }

  symbols = [...new Set(symbols)].slice(0, 2000);
  if (!symbols.length) return json({ error: "no symbols" }, 400);

  try {
    const quotes =
      provider === "polygon" ? await polygonQuotes(symbols) : await yahooQuotes(symbols);
    return json(
      { provider, count: quotes.length, asOf: new Date().toISOString(), quotes },
      200,
      provider === "yahoo" ? "public, max-age=20" : "no-store"
    );
  } catch (e) {
    return json({ error: String(e.message || e), provider }, 502);
  }
};

function json(obj, status = 200, cache = "no-store") {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", "cache-control": cache },
  });
}

export const config = { path: "/api/quotes" };
