/**
 * Binance Price Alert - alert storage API (Cloudflare Worker + D1).
 *
 * Two independent credentials:
 *   ADMIN_TOKEN   - used by the web UI (/api/*): create, list, edit, cancel, delete alerts.
 *   CHECKER_TOKEN - used by the GitHub Actions checker (/internal/*): read work, record
 *                   prices, claim triggers and record notification results.
 *
 * All state transitions are single conditional UPDATE ... RETURNING statements, so two
 * concurrent checker runs (or a checker run racing a user edit) can never both claim
 * the same alert.
 */

const PRICE_RE = /^(?:0|[1-9]\d{0,11})(?:\.\d{1,8})?$/;
const OBSERVED_PRICE_RE = /^(?:0|[1-9]\d{0,15})(?:\.\d{1,18})?$/;
const SYMBOL_RE = /^[A-Z0-9]{2,20}$/;
const ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const CONDITIONS = new Set(["above", "below"]);
const MAX_ACTIVE_ALERTS = 200;
const MAX_BODY_BYTES = 64 * 1024;
const MAX_OBSERVATIONS = 500;
const DEFAULT_LEASE_SECONDS = 600;
const MAX_ERROR_LENGTH = 300;
const BINANCE_TIMEOUT_MS = 8000;
const DEFAULT_BINANCE_BASE_URLS = ["https://data-api.binance.vision", "https://api.binance.com"];

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// ---------------------------------------------------------------- helpers

function nowIso() {
  return new Date().toISOString();
}

function addSecondsIso(seconds) {
  return new Date(Date.now() + seconds * 1000).toISOString();
}

/** Validates a user-supplied target price and returns a normalised decimal string. */
export function normalizePrice(value, pattern = PRICE_RE) {
  if (typeof value === "number" && Number.isFinite(value)) value = String(value);
  if (typeof value !== "string") throw new HttpError(400, "Price must be a decimal string.");
  const text = value.trim();
  if (!pattern.test(text)) {
    throw new HttpError(400, "Price must be a positive decimal number with at most 8 decimal places.");
  }
  let [whole, frac = ""] = text.split(".");
  frac = frac.replace(/0+$/, "");
  if (/^0*$/.test(whole) && frac === "") throw new HttpError(400, "Price must be greater than zero.");
  return frac ? `${whole}.${frac}` : whole;
}

export function normalizeSymbol(value) {
  if (typeof value !== "string") throw new HttpError(400, "Symbol is required.");
  const symbol = value.trim().toUpperCase().replace("/", "");
  if (!SYMBOL_RE.test(symbol)) throw new HttpError(400, "Symbol format is invalid.");
  return symbol;
}

function normalizeCondition(value) {
  const condition = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (!CONDITIONS.has(condition)) throw new HttpError(400, "Condition must be 'above' or 'below'.");
  return condition;
}

function requireId(id) {
  if (!ID_RE.test(id)) throw new HttpError(404, "Alert not found.");
  return id;
}

function truncate(text, max = MAX_ERROR_LENGTH) {
  if (text == null) return null;
  const s = String(text);
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/** Constant-time string comparison (avoids leaking token prefixes through timing). */
function safeEqual(a, b) {
  const enc = new TextEncoder();
  const x = enc.encode(String(a));
  const y = enc.encode(String(b));
  let diff = x.length ^ y.length;
  const len = Math.max(x.length, y.length);
  for (let i = 0; i < len; i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

function checkToken(request, expected) {
  if (!expected || expected.length < 16) {
    throw new HttpError(500, "Server is not configured: missing access token secret.");
  }
  const header = request.headers.get("Authorization") || "";
  const match = header.match(/^Bearer\s+(.+)$/i);
  if (!match || !safeEqual(match[1].trim(), expected)) {
    throw new HttpError(401, "Invalid or missing access key.");
  }
}

async function readJson(request) {
  const length = Number(request.headers.get("Content-Length") || 0);
  if (length > MAX_BODY_BYTES) throw new HttpError(413, "Request body too large.");
  const text = await request.text();
  if (text.length > MAX_BODY_BYTES) throw new HttpError(413, "Request body too large.");
  if (!text) return {};
  try {
    const data = JSON.parse(text);
    if (data === null || typeof data !== "object" || Array.isArray(data)) throw new Error();
    return data;
  } catch {
    throw new HttpError(400, "Request body must be a JSON object.");
  }
}

function allowedOrigins(env) {
  return (env.ALLOWED_ORIGINS || "")
    .split(",")
    .map((o) => o.trim().replace(/\/+$/, ""))
    .filter(Boolean);
}

function corsHeaders(request, env) {
  const origin = request.headers.get("Origin");
  if (!origin || !allowedOrigins(env).includes(origin)) return {};
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function json(data, status, extraHeaders) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      ...extraHeaders,
    },
  });
}

// ---------------------------------------------------------------- Binance lookup

/**
 * Confirms a symbol is an actively trading Binance Spot pair.
 * Returns {symbol, baseAsset, quoteAsset}, or throws HttpError 400 / 503.
 */
export async function lookupSymbol(symbol, fetchImpl, baseUrls = DEFAULT_BINANCE_BASE_URLS) {
  let lastProblem = "no endpoint reachable";
  for (const base of baseUrls) {
    let res;
    try {
      res = await fetchImpl(`${base}/api/v3/exchangeInfo?symbol=${encodeURIComponent(symbol)}`, {
        signal: AbortSignal.timeout(BINANCE_TIMEOUT_MS),
      });
    } catch (err) {
      lastProblem = `network error (${err?.name || "Error"})`;
      continue;
    }
    if (res.status === 400) {
      const body = await res.json().catch(() => ({}));
      if (body.code === -1121) throw new HttpError(400, `${symbol} is not a valid Binance trading pair.`);
      lastProblem = `HTTP 400 (${body.code ?? "unknown"})`;
      continue;
    }
    if (!res.ok) {
      lastProblem = `HTTP ${res.status}`;
      continue;
    }
    const body = await res.json().catch(() => null);
    const info = body?.symbols?.find((s) => s.symbol === symbol);
    if (!info) throw new HttpError(400, `${symbol} is not a valid Binance trading pair.`);
    if (info.status !== "TRADING" || info.isSpotTradingAllowed === false) {
      throw new HttpError(400, `${symbol} is not currently trading on Binance Spot.`);
    }
    return { symbol, baseAsset: info.baseAsset, quoteAsset: info.quoteAsset };
  }
  throw new HttpError(503, `Could not verify the symbol with Binance (${lastProblem}). Try again shortly.`);
}

const ASSET_RE = /^[A-Z0-9]{1,20}$/;

/** Base/quote supplied by the client, accepted only if they spell out the symbol exactly. */
export function unverifiedSymbolInfo(symbol, baseAsset, quoteAsset) {
  const base = typeof baseAsset === "string" ? baseAsset.trim().toUpperCase() : "";
  const quote = typeof quoteAsset === "string" ? quoteAsset.trim().toUpperCase() : "";
  if (!ASSET_RE.test(base) || !ASSET_RE.test(quote) || base + quote !== symbol) return null;
  return { symbol, baseAsset: base, quoteAsset: quote };
}

// ---------------------------------------------------------------- public API (web UI)

async function listAlerts(env) {
  const { results } = await env.DB.prepare(
    "SELECT * FROM alerts ORDER BY created_at DESC LIMIT 500"
  ).all();
  return json({ alerts: results }, 200);
}

async function createAlert(request, env, deps) {
  const body = await readJson(request);
  const symbol = normalizeSymbol(body.symbol);
  const targetPrice = normalizePrice(body.target_price);
  const condition = normalizeCondition(body.condition);

  const active = await env.DB.prepare("SELECT COUNT(*) AS n FROM alerts WHERE status = 'active'").first();
  if ((active?.n ?? 0) >= MAX_ACTIVE_ALERTS) {
    throw new HttpError(409, `You already have ${MAX_ACTIVE_ALERTS} active alerts. Cancel or delete some first.`);
  }

  const baseUrls = env.BINANCE_BASE_URLS
    ? env.BINANCE_BASE_URLS.split(",").map((s) => s.trim()).filter(Boolean)
    : DEFAULT_BINANCE_BASE_URLS;
  let info;
  try {
    info = await lookupSymbol(symbol, deps.fetchImpl, baseUrls);
  } catch (err) {
    // Binance blocks some Cloudflare egress IPs (HTTP 403/451). The browser already validated the
    // pair against Binance from the user's own connection, so accept its base/quote split when it
    // is consistent; the scheduled checker reports the alert if the pair turns out not to exist.
    if (!(err instanceof HttpError) || err.status !== 503) throw err;
    info = unverifiedSymbolInfo(symbol, body.base_asset, body.quote_asset);
    if (!info) throw err;
  }

  const now = nowIso();
  try {
    const alert = await env.DB.prepare(
      `INSERT INTO alerts (id, symbol, base_asset, quote_asset, target_price, condition,
                           status, notification_status, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, 'active', 'none', ?, ?)
       RETURNING *`
    )
      .bind(crypto.randomUUID(), symbol, info.baseAsset, info.quoteAsset, targetPrice, condition, now, now)
      .first();
    return json({ alert }, 201);
  } catch (err) {
    if (/UNIQUE/i.test(String(err?.message))) {
      throw new HttpError(409, "An identical active alert already exists.");
    }
    throw err;
  }
}

async function explainMiss(env, id, conflictMessage) {
  const row = await env.DB.prepare("SELECT status FROM alerts WHERE id = ?").bind(id).first();
  if (!row) throw new HttpError(404, "Alert not found.");
  throw new HttpError(409, conflictMessage);
}

async function updateAlert(request, env, id) {
  const body = await readJson(request);
  const version = Number(body.version);
  if (!Number.isInteger(version) || version < 1) throw new HttpError(400, "version is required.");
  if (body.target_price === undefined && body.condition === undefined) {
    throw new HttpError(400, "Nothing to update.");
  }
  const targetPrice = body.target_price === undefined ? null : normalizePrice(body.target_price);
  const condition = body.condition === undefined ? null : normalizeCondition(body.condition);
  try {
    const alert = await env.DB.prepare(
      `UPDATE alerts
          SET target_price = COALESCE(?, target_price),
              condition    = COALESCE(?, condition),
              last_error   = NULL,
              version      = version + 1,
              updated_at   = ?
        WHERE id = ? AND status = 'active' AND version = ?
        RETURNING *`
    )
      .bind(targetPrice, condition, nowIso(), id, version)
      .first();
    if (alert) return json({ alert }, 200);
  } catch (err) {
    if (/UNIQUE/i.test(String(err?.message))) {
      throw new HttpError(409, "An identical active alert already exists.");
    }
    throw err;
  }
  return explainMiss(env, id, "This alert changed or is no longer active. Refresh and try again.");
}

async function cancelAlert(env, id) {
  const alert = await env.DB.prepare(
    `UPDATE alerts SET status = 'cancelled', version = version + 1, updated_at = ?
      WHERE id = ? AND status = 'active'
      RETURNING *`
  )
    .bind(nowIso(), id)
    .first();
  if (alert) return json({ alert }, 200);
  return explainMiss(env, id, "Only active alerts can be cancelled.");
}

async function retryNotification(env, id) {
  const alert = await env.DB.prepare(
    `UPDATE alerts
        SET notification_status = 'pending', notification_attempts = 0, notification_error = NULL,
            version = version + 1, updated_at = ?
      WHERE id = ? AND status = 'triggered' AND notification_status = 'failed'
      RETURNING *`
  )
    .bind(nowIso(), id)
    .first();
  if (alert) return json({ alert }, 200);
  return explainMiss(env, id, "Only alerts whose notification failed can be retried.");
}

async function deleteAlert(env, id) {
  const result = await env.DB.prepare("DELETE FROM alerts WHERE id = ?").bind(id).run();
  if (!result.meta?.changes) throw new HttpError(404, "Alert not found.");
  return json({ deleted: true }, 200);
}

// ---------------------------------------------------------------- internal API (checker)

async function getWork(env) {
  const now = nowIso();
  const active = await env.DB.prepare("SELECT * FROM alerts WHERE status = 'active'").all();
  const notifications = await env.DB.prepare(
    `SELECT * FROM alerts
      WHERE status = 'triggered'
        AND (notification_status IN ('pending', 'failed')
             OR (notification_status = 'sending' AND notification_lease_until < ?))`
  )
    .bind(now)
    .all();
  return json({ active: active.results, pending_notifications: notifications.results, server_time: now }, 200);
}

async function recordObservations(request, env) {
  const body = await readJson(request);
  const items = body.observations;
  if (!Array.isArray(items) || items.length > MAX_OBSERVATIONS) {
    throw new HttpError(400, `observations must be an array of at most ${MAX_OBSERVATIONS} items.`);
  }
  const now = nowIso();
  const statements = items.map((item) => {
    const id = requireId(String(item?.id ?? ""));
    const price = item.price == null ? null : normalizePrice(String(item.price), OBSERVED_PRICE_RE);
    return env.DB.prepare(
      `UPDATE alerts
          SET last_price = COALESCE(?, last_price), last_checked_at = ?, last_error = ?
        WHERE id = ? AND status = 'active'`
    ).bind(price, now, truncate(item.error), id);
  });
  if (statements.length) await env.DB.batch(statements);
  return json({ updated: statements.length }, 200);
}

function leaseSeconds(body) {
  const n = Number(body.lease_seconds ?? DEFAULT_LEASE_SECONDS);
  return Number.isFinite(n) && n >= 30 && n <= 3600 ? Math.floor(n) : DEFAULT_LEASE_SECONDS;
}

async function claimTrigger(request, env, id) {
  const body = await readJson(request);
  const version = Number(body.version);
  if (!Number.isInteger(version)) throw new HttpError(400, "version is required.");
  const price = normalizePrice(String(body.price ?? ""), OBSERVED_PRICE_RE);
  const now = nowIso();
  const alert = await env.DB.prepare(
    `UPDATE alerts
        SET status = 'triggered',
            triggered_price = ?, triggered_at = ?,
            last_price = ?, last_checked_at = ?, last_error = NULL,
            notification_status = 'sending', notification_attempts = 1,
            notification_lease_until = ?, notification_error = NULL,
            version = version + 1, updated_at = ?
      WHERE id = ? AND status = 'active' AND version = ?
      RETURNING *`
  )
    .bind(price, now, price, now, addSecondsIso(leaseSeconds(body)), now, id, version)
    .first();
  if (alert) return json({ claimed: true, alert }, 200);
  return json({ claimed: false }, 409);
}

async function claimNotification(request, env, id) {
  const body = await readJson(request);
  const maxAttempts = Number(body.max_attempts ?? 5);
  if (!Number.isInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > 50) {
    throw new HttpError(400, "max_attempts must be an integer between 1 and 50.");
  }
  const now = nowIso();
  const alert = await env.DB.prepare(
    `UPDATE alerts
        SET notification_status = 'sending',
            notification_attempts = notification_attempts + 1,
            notification_lease_until = ?,
            version = version + 1, updated_at = ?
      WHERE id = ? AND status = 'triggered'
        AND notification_attempts < ?
        AND (notification_status IN ('pending', 'failed')
             OR (notification_status = 'sending' AND notification_lease_until < ?))
      RETURNING *`
  )
    .bind(addSecondsIso(leaseSeconds(body)), now, id, maxAttempts, now)
    .first();
  if (alert) return json({ claimed: true, alert }, 200);
  return json({ claimed: false }, 409);
}

async function recordNotificationResult(request, env, id) {
  const body = await readJson(request);
  if (body.result !== "sent" && body.result !== "failed") {
    throw new HttpError(400, "result must be 'sent' or 'failed'.");
  }
  const now = nowIso();
  const stmt =
    body.result === "sent"
      ? env.DB.prepare(
          `UPDATE alerts
              SET notification_status = 'sent', notified_at = ?, notification_error = NULL,
                  notification_lease_until = NULL, version = version + 1, updated_at = ?
            WHERE id = ? AND notification_status = 'sending'
            RETURNING *`
        ).bind(now, now, id)
      : env.DB.prepare(
          `UPDATE alerts
              SET notification_status = 'failed', notification_error = ?,
                  notification_lease_until = NULL, version = version + 1, updated_at = ?
            WHERE id = ? AND notification_status = 'sending'
            RETURNING *`
        ).bind(truncate(body.error) || "Unknown error", now, id);
  const alert = await stmt.first();
  if (alert) return json({ alert }, 200);
  return explainMiss(env, id, "Notification is not in the 'sending' state.");
}

// ---------------------------------------------------------------- router

async function route(request, env, deps) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const method = request.method;
  const parts = path.split("/").filter(Boolean);

  if (path === "/api/health" && method === "GET") return json({ ok: true, time: nowIso() }, 200);

  if (parts[0] === "api" && parts[1] === "alerts") {
    checkToken(request, env.ADMIN_TOKEN);
    if (parts.length === 2) {
      if (method === "GET") return listAlerts(env);
      if (method === "POST") return createAlert(request, env, deps);
    } else if (parts.length === 3) {
      const id = requireId(parts[2]);
      if (method === "PATCH") return updateAlert(request, env, id);
      if (method === "DELETE") return deleteAlert(env, id);
    } else if (parts.length === 4 && method === "POST") {
      const id = requireId(parts[2]);
      if (parts[3] === "cancel") return cancelAlert(env, id);
      if (parts[3] === "retry-notification") return retryNotification(env, id);
    }
    throw new HttpError(405, "Method not allowed.");
  }

  if (parts[0] === "internal") {
    checkToken(request, env.CHECKER_TOKEN);
    if (path === "/internal/work" && method === "GET") return getWork(env);
    if (path === "/internal/observations" && method === "POST") return recordObservations(request, env);
    if (parts[1] === "alerts" && method === "POST") {
      const id = requireId(parts[2] || "");
      const action = parts.slice(3).join("/");
      if (action === "trigger") return claimTrigger(request, env, id);
      if (action === "notification/claim") return claimNotification(request, env, id);
      if (action === "notification/result") return recordNotificationResult(request, env, id);
    }
    throw new HttpError(404, "Not found.");
  }

  throw new HttpError(404, "Not found.");
}

export function createApp(deps = {}) {
  const resolved = { fetchImpl: deps.fetchImpl || ((...args) => fetch(...args)) };
  return {
    async fetch(request, env) {
      const cors = corsHeaders(request, env);
      if (request.method === "OPTIONS") {
        return new Response(null, { status: Object.keys(cors).length ? 204 : 403, headers: cors });
      }
      try {
        const response = await route(request, env, resolved);
        for (const [k, v] of Object.entries(cors)) response.headers.set(k, v);
        return response;
      } catch (err) {
        if (err instanceof HttpError) return json({ error: err.message }, err.status, cors);
        console.error("Unhandled error:", err?.message || err);
        return json({ error: "Internal server error." }, 500, cors);
      }
    },
  };
}

export default createApp();
