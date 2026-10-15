// Tests for the alert storage API (worker/src/index.js).
// Run with: node --test tests/worker.test.mjs   (Node.js 22.13+; uses built-in node:sqlite)
//
// A tiny in-memory shim implements the subset of the Cloudflare D1 API the worker uses,
// backed by real SQLite, so the actual SQL (constraints, conditional updates) is exercised.

import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { createApp, dispatchChecker } from "../worker/src/index.js";

const SCHEMA = readFileSync(new URL("../worker/schema.sql", import.meta.url), "utf8");
const ADMIN = "admin-token-0123456789abcdef";
const CHECKER = "checker-token-0123456789abcdef";
const ORIGIN = "https://me.github.io";

class Statement {
  constructor(db, sql) {
    this.db = db;
    this.sql = sql;
    this.args = [];
  }
  bind(...args) {
    this.args = args;
    return this;
  }
  async first() {
    return this.db.prepare(this.sql).get(...this.args) ?? null;
  }
  async all() {
    return { results: this.db.prepare(this.sql).all(...this.args) };
  }
  async run() {
    const r = this.db.prepare(this.sql).run(...this.args);
    return { success: true, meta: { changes: Number(r.changes) } };
  }
}

class D1Shim {
  constructor() {
    this.db = new DatabaseSync(":memory:");
    this.db.exec(SCHEMA);
  }
  prepare(sql) {
    return new Statement(this.db, sql);
  }
  async batch(statements) {
    this.db.exec("BEGIN");
    try {
      const out = [];
      for (const s of statements) out.push(await s.run());
      this.db.exec("COMMIT");
      return out;
    } catch (err) {
      this.db.exec("ROLLBACK");
      throw err;
    }
  }
}

function binanceFetch(symbols = { XRPUSDT: ["XRP", "USDT", "TRADING"], BTCUSDT: ["BTC", "USDT", "TRADING"] }) {
  const calls = [];
  const impl = async (url) => {
    calls.push(url);
    const symbol = new URL(url).searchParams.get("symbol");
    const info = symbols[symbol];
    if (!info) return new Response(JSON.stringify({ code: -1121, msg: "Invalid symbol." }), { status: 400 });
    return new Response(
      JSON.stringify({ symbols: [{ symbol, baseAsset: info[0], quoteAsset: info[1], status: info[2], isSpotTradingAllowed: true }] }),
      { status: 200 }
    );
  };
  impl.calls = calls;
  return impl;
}

let env;
let app;

beforeEach(() => {
  env = { DB: new D1Shim(), ADMIN_TOKEN: ADMIN, CHECKER_TOKEN: CHECKER, ALLOWED_ORIGINS: ORIGIN };
  app = createApp({ fetchImpl: binanceFetch() });
});

async function call(method, path, { token = ADMIN, body, origin } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (origin) headers.Origin = origin;
  const res = await app.fetch(
    new Request(`https://worker.test${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) }),
    env
  );
  const text = await res.text();
  return { status: res.status, body: text ? JSON.parse(text) : null, headers: res.headers };
}

async function createAlert(overrides = {}) {
  const res = await call("POST", "/api/alerts", {
    body: { symbol: "XRP/USDT", target_price: "2.50", condition: "above", ...overrides },
  });
  assert.equal(res.status, 201, JSON.stringify(res.body));
  return res.body.alert;
}

// ------------------------------------------------------------------ auth & CORS

test("rejects requests without the correct access key", async () => {
  assert.equal((await call("GET", "/api/alerts", { token: null })).status, 401);
  assert.equal((await call("GET", "/api/alerts", { token: "wrong-token-wrong-token" })).status, 401);
  // The checker token must not grant access to the admin API, and vice versa.
  assert.equal((await call("GET", "/api/alerts", { token: CHECKER })).status, 401);
  assert.equal((await call("GET", "/internal/work", { token: ADMIN })).status, 401);
});

test("refuses to run when secrets are missing", async () => {
  env.ADMIN_TOKEN = "";
  assert.equal((await call("GET", "/api/alerts")).status, 500);
});

test("CORS headers only for allowed origins", async () => {
  const ok = await call("GET", "/api/alerts", { origin: ORIGIN });
  assert.equal(ok.headers.get("Access-Control-Allow-Origin"), ORIGIN);
  const bad = await call("GET", "/api/alerts", { origin: "https://evil.example" });
  assert.equal(bad.headers.get("Access-Control-Allow-Origin"), null);
  const preflight = await app.fetch(new Request("https://worker.test/api/alerts", { method: "OPTIONS", headers: { Origin: "https://evil.example" } }), env);
  assert.equal(preflight.status, 403);
});

// ------------------------------------------------------------------ create / read

test("creates and lists an alert (persistent creation and retrieval)", async () => {
  const alert = await createAlert();
  assert.equal(alert.symbol, "XRPUSDT");
  assert.equal(alert.base_asset, "XRP");
  assert.equal(alert.quote_asset, "USDT");
  assert.equal(alert.target_price, "2.5");
  assert.equal(alert.status, "active");
  assert.equal(alert.notification_status, "none");
  const list = await call("GET", "/api/alerts");
  assert.equal(list.status, 200);
  assert.deepEqual(list.body.alerts.map((a) => a.id), [alert.id]);
});

test("rejects invalid target prices", async () => {
  for (const price of ["0", "0.000", "-1", "abc", "", "1e5", "1.123456789", null, "01.5"]) {
    const res = await call("POST", "/api/alerts", { body: { symbol: "XRPUSDT", target_price: price, condition: "above" } });
    assert.equal(res.status, 400, `price ${JSON.stringify(price)} should be rejected`);
  }
});

test("rejects invalid conditions and malformed bodies", async () => {
  assert.equal((await call("POST", "/api/alerts", { body: { symbol: "XRPUSDT", target_price: "1", condition: "sideways" } })).status, 400);
  const res = await app.fetch(
    new Request("https://worker.test/api/alerts", { method: "POST", headers: { Authorization: `Bearer ${ADMIN}` }, body: "{not json" }),
    env
  );
  assert.equal(res.status, 400);
});

test("rejects invalid trading symbols", async () => {
  assert.equal((await call("POST", "/api/alerts", { body: { symbol: "FAKEUSDT", target_price: "1", condition: "above" } })).status, 400);
  assert.equal((await call("POST", "/api/alerts", { body: { symbol: "<script>", target_price: "1", condition: "above" } })).status, 400);
});

test("rejects pairs that are not trading", async () => {
  app = createApp({ fetchImpl: binanceFetch({ OLDUSDT: ["OLD", "USDT", "BREAK"] }) });
  assert.equal((await call("POST", "/api/alerts", { body: { symbol: "OLDUSDT", target_price: "1", condition: "above" } })).status, 400);
});

test("returns 503 when Binance cannot be reached, falling back across hosts", async () => {
  const urls = [];
  app = createApp({
    fetchImpl: async (url) => {
      urls.push(new URL(url).host);
      return new Response("restricted", { status: 451 });
    },
  });
  const res = await call("POST", "/api/alerts", { body: { symbol: "XRPUSDT", target_price: "1", condition: "above" } });
  assert.equal(res.status, 503);
  assert.deepEqual(urls, ["data-api.binance.vision", "api.binance.com"]);
});

test("accepts the browser-validated base/quote when Binance blocks the Worker", async () => {
  app = createApp({ fetchImpl: async () => new Response("forbidden", { status: 403 }) });
  const res = await call("POST", "/api/alerts", {
    body: { symbol: "XRPUSDT", base_asset: "XRP", quote_asset: "USDT", target_price: "2.5", condition: "above" },
  });
  assert.equal(res.status, 201);
  const { alert } = res.body;
  assert.equal(alert.symbol, "XRPUSDT");
  assert.equal(alert.base_asset, "XRP");
  assert.equal(alert.quote_asset, "USDT");
});

test("rejects an inconsistent base/quote when Binance blocks the Worker", async () => {
  app = createApp({ fetchImpl: async () => new Response("forbidden", { status: 403 }) });
  const res = await call("POST", "/api/alerts", {
    body: { symbol: "XRPUSDT", base_asset: "BTC", quote_asset: "USDT", target_price: "2.5", condition: "above" },
  });
  assert.equal(res.status, 503);
});

test("prevents duplicate active alerts", async () => {
  await createAlert();
  const dup = await call("POST", "/api/alerts", { body: { symbol: "XRPUSDT", target_price: "2.500", condition: "above" } });
  assert.equal(dup.status, 409);
});

// ------------------------------------------------------------------ edit / cancel / delete

test("edits an active alert with optimistic concurrency", async () => {
  const alert = await createAlert();
  const ok = await call("PATCH", `/api/alerts/${alert.id}`, { body: { target_price: "3", condition: "below", version: alert.version } });
  assert.equal(ok.status, 200);
  assert.equal(ok.body.alert.target_price, "3");
  assert.equal(ok.body.alert.condition, "below");
  const stale = await call("PATCH", `/api/alerts/${alert.id}`, { body: { target_price: "4", version: alert.version } });
  assert.equal(stale.status, 409);
});

test("cancels and deletes alerts", async () => {
  const alert = await createAlert();
  const cancelled = await call("POST", `/api/alerts/${alert.id}/cancel`);
  assert.equal(cancelled.body.alert.status, "cancelled");
  assert.equal((await call("POST", `/api/alerts/${alert.id}/cancel`)).status, 409);
  assert.equal((await call("PATCH", `/api/alerts/${alert.id}`, { body: { target_price: "9", version: cancelled.body.alert.version } })).status, 409);
  assert.equal((await call("DELETE", `/api/alerts/${alert.id}`)).status, 200);
  assert.equal((await call("DELETE", `/api/alerts/${alert.id}`)).status, 404);
  assert.equal((await call("POST", "/api/alerts/not-a-uuid/cancel")).status, 404);
});

// ------------------------------------------------------------------ checker workflow

test("checker reads active alerts but not cancelled ones", async () => {
  const a = await createAlert();
  const b = await createAlert({ symbol: "BTCUSDT", target_price: "50000", condition: "below" });
  await call("POST", `/api/alerts/${b.id}/cancel`);
  const work = await call("GET", "/internal/work", { token: CHECKER });
  assert.deepEqual(work.body.active.map((x) => x.id), [a.id]);
  assert.deepEqual(work.body.pending_notifications, []);
});

test("records observations for active alerts", async () => {
  const a = await createAlert();
  const res = await call("POST", "/internal/observations", { token: CHECKER, body: { observations: [{ id: a.id, price: "2.40000000" }] } });
  assert.equal(res.status, 200);
  const [row] = (await call("GET", "/api/alerts")).body.alerts;
  assert.equal(row.last_price, "2.4");
  assert.ok(row.last_checked_at);
});

test("trigger claim is atomic: a second (concurrent) run cannot claim the same alert", async () => {
  const a = await createAlert();
  const first = await call("POST", `/internal/alerts/${a.id}/trigger`, { token: CHECKER, body: { version: a.version, price: "2.51" } });
  assert.equal(first.status, 200);
  assert.equal(first.body.alert.status, "triggered");
  assert.equal(first.body.alert.notification_status, "sending");
  assert.equal(first.body.alert.notification_attempts, 1);
  const second = await call("POST", `/internal/alerts/${a.id}/trigger`, { token: CHECKER, body: { version: a.version, price: "2.51" } });
  assert.equal(second.status, 409);
  assert.equal(second.body.claimed, false);
});

test("trigger claim fails if the user edited the alert after the checker read it", async () => {
  const a = await createAlert();
  await call("PATCH", `/api/alerts/${a.id}`, { body: { target_price: "9", version: a.version } });
  const res = await call("POST", `/internal/alerts/${a.id}/trigger`, { token: CHECKER, body: { version: a.version, price: "2.51" } });
  assert.equal(res.status, 409);
});

test("successful delivery is recorded once and never re-queued", async () => {
  const a = await createAlert();
  await call("POST", `/internal/alerts/${a.id}/trigger`, { token: CHECKER, body: { version: a.version, price: "2.51" } });
  const sent = await call("POST", `/internal/alerts/${a.id}/notification/result`, { token: CHECKER, body: { result: "sent" } });
  assert.equal(sent.body.alert.notification_status, "sent");
  assert.ok(sent.body.alert.notified_at);
  const work = await call("GET", "/internal/work", { token: CHECKER });
  assert.equal(work.body.active.length, 0);
  assert.equal(work.body.pending_notifications.length, 0);
  const claim = await call("POST", `/internal/alerts/${a.id}/notification/claim`, { token: CHECKER, body: { max_attempts: 5 } });
  assert.equal(claim.status, 409);
});

test("failed delivery is queued for retry until max attempts", async () => {
  const a = await createAlert();
  await call("POST", `/internal/alerts/${a.id}/trigger`, { token: CHECKER, body: { version: a.version, price: "2.51" } });
  await call("POST", `/internal/alerts/${a.id}/notification/result`, { token: CHECKER, body: { result: "failed", error: "HTTP 502" } });
  let work = await call("GET", "/internal/work", { token: CHECKER });
  assert.equal(work.body.pending_notifications.length, 1);
  assert.equal(work.body.pending_notifications[0].notification_error, "HTTP 502");

  const claim = await call("POST", `/internal/alerts/${a.id}/notification/claim`, { token: CHECKER, body: { max_attempts: 2 } });
  assert.equal(claim.status, 200);
  assert.equal(claim.body.alert.notification_attempts, 2);
  // While a lease is held, a concurrent run cannot claim it.
  assert.equal((await call("POST", `/internal/alerts/${a.id}/notification/claim`, { token: CHECKER, body: { max_attempts: 5 } })).status, 409);
  await call("POST", `/internal/alerts/${a.id}/notification/result`, { token: CHECKER, body: { result: "failed", error: "again" } });
  // Attempts exhausted.
  assert.equal((await call("POST", `/internal/alerts/${a.id}/notification/claim`, { token: CHECKER, body: { max_attempts: 2 } })).status, 409);

  // The user can manually reset and retry from the UI.
  const retry = await call("POST", `/api/alerts/${a.id}/retry-notification`);
  assert.equal(retry.status, 200);
  assert.equal(retry.body.alert.notification_status, "pending");
  assert.equal(retry.body.alert.notification_attempts, 0);
  work = await call("GET", "/internal/work", { token: CHECKER });
  assert.equal(work.body.pending_notifications.length, 1);
});

test("an expired 'sending' lease (crashed run) is picked up again", async () => {
  const a = await createAlert();
  await call("POST", `/internal/alerts/${a.id}/trigger`, { token: CHECKER, body: { version: a.version, price: "2.51" } });
  env.DB.db.prepare("UPDATE alerts SET notification_lease_until = '2000-01-01T00:00:00.000Z'").run();
  const work = await call("GET", "/internal/work", { token: CHECKER });
  assert.equal(work.body.pending_notifications.length, 1);
  const claim = await call("POST", `/internal/alerts/${a.id}/notification/claim`, { token: CHECKER, body: { max_attempts: 5 } });
  assert.equal(claim.status, 200);
});

test("cannot retry a notification that has not failed", async () => {
  const a = await createAlert();
  assert.equal((await call("POST", `/api/alerts/${a.id}/retry-notification`)).status, 409);
});

test("health endpoint needs no auth", async () => {
  const res = await call("GET", "/api/health", { token: null });
  assert.equal(res.status, 200);
  assert.equal(res.body.ok, true);
});

function recordingFetch(status = 204) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return new Response(null, { status });
  };
  return { calls, fetchImpl };
}

test("cron: dispatches the checker workflow when there are active alerts", async () => {
  await createAlert();
  const { calls, fetchImpl } = recordingFetch();
  const cronEnv = { ...env, GITHUB_REPO: "me/repo", GITHUB_DISPATCH_TOKEN: "gh-token" };
  assert.equal(await dispatchChecker(cronEnv, fetchImpl), "dispatched");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.github.com/repos/me/repo/actions/workflows/price-alert.yml/dispatches");
  assert.equal(calls[0].init.headers.Authorization, "Bearer gh-token");
  assert.deepEqual(JSON.parse(calls[0].init.body), { ref: "main", inputs: { mode: "check" } });
});

test("cron: skips the dispatch when nothing needs checking", async () => {
  const { calls, fetchImpl } = recordingFetch();
  const cronEnv = { ...env, GITHUB_REPO: "me/repo", GITHUB_DISPATCH_TOKEN: "gh-token" };
  assert.equal(await dispatchChecker(cronEnv, fetchImpl), "idle");
  assert.equal(calls.length, 0);
});

test("cron: does nothing without a dispatch token and reports GitHub errors", async () => {
  await createAlert();
  const { calls, fetchImpl } = recordingFetch(401);
  assert.equal(await dispatchChecker(env, fetchImpl), "not-configured");
  assert.equal(calls.length, 0);
  const cronEnv = { ...env, GITHUB_REPO: "me/repo", GITHUB_DISPATCH_TOKEN: "bad" };
  assert.equal(await dispatchChecker(cronEnv, fetchImpl), "error-401");
});
