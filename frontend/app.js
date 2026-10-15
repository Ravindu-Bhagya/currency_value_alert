/* Binance Price Alert - web client.
 *
 * - Market data comes straight from Binance's public API (no key).
 * - Alerts are stored by your Cloudflare Worker; requests carry the access key you enter
 *   in the Connect dialog. No secret is embedded in this file.
 * - All dynamic text is inserted with textContent (never innerHTML) to prevent XSS.
 */
(() => {
  "use strict";

  const CONFIG = Object.assign(
    {
      apiBaseUrl: "",
      binanceBaseUrls: ["https://data-api.binance.vision", "https://api.binance.com"],
      priceRefreshSeconds: 15,
      alertsRefreshSeconds: 60,
      maxNotificationAttempts: 5,
    },
    window.APP_CONFIG || {}
  );
  const API_BASE = String(CONFIG.apiBaseUrl || "").replace(/\/+$/, "");
  const API_CONFIGURED = /^(https:\/\/[^/]+|http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$)/.test(API_BASE) && !/YOUR-SUBDOMAIN/i.test(API_BASE);

  const KEY_STORAGE = "bpa.accessKey";
  const SYMBOLS_CACHE = "bpa.symbols.v1";
  const SYMBOLS_TTL_MS = 12 * 60 * 60 * 1000;
  const PRICE_RE = /^(?:0|[1-9]\d{0,11})(?:\.\d{1,8})?$/;
  const MAX_RESULTS = 50;
  const QUOTE_RANK = ["USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB", "EUR", "TRY", "BRL", "JPY"];
  const POPULAR = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT"];

  // Binance's exchangeInfo has no coin names, so common names are mapped here for search.
  const COIN_NAMES = {
    BTC: "Bitcoin", ETH: "Ethereum", XRP: "XRP Ripple", SOL: "Solana", BNB: "BNB Binance Coin",
    DOGE: "Dogecoin", ADA: "Cardano", TRX: "TRON", TON: "Toncoin", AVAX: "Avalanche",
    SHIB: "Shiba Inu", DOT: "Polkadot", LINK: "Chainlink", BCH: "Bitcoin Cash", LTC: "Litecoin",
    NEAR: "NEAR Protocol", POL: "Polygon", MATIC: "Polygon", UNI: "Uniswap", ICP: "Internet Computer",
    ETC: "Ethereum Classic", APT: "Aptos", XLM: "Stellar", ATOM: "Cosmos", FIL: "Filecoin",
    HBAR: "Hedera", ARB: "Arbitrum", OP: "Optimism", SUI: "Sui", PEPE: "Pepe", AAVE: "Aave",
    INJ: "Injective", RENDER: "Render", IMX: "Immutable", VET: "VeChain", ALGO: "Algorand",
    SEI: "Sei", TIA: "Celestia", WIF: "dogwifhat", FET: "Fetch.ai Artificial Superintelligence",
    GRT: "The Graph", SAND: "The Sandbox", MANA: "Decentraland", AXS: "Axie Infinity",
    EGLD: "MultiversX", XTZ: "Tezos", EOS: "EOS", THETA: "Theta Network", FLOW: "Flow",
    ENA: "Ethena", JUP: "Jupiter", BONK: "Bonk", FLOKI: "Floki", ONDO: "Ondo", TAO: "Bittensor",
    USDC: "USD Coin", FDUSD: "First Digital USD", ZEC: "Zcash", DASH: "Dash", XMR: "Monero",
  };

  // ------------------------------------------------------------------ DOM helpers

  const $ = (id) => document.getElementById(id);
  const els = {
    banner: $("setup-banner"),
    connectBtn: $("connect-btn"), connectDot: $("connect-dot"), connectLabel: $("connect-label"),
    form: $("alert-form"),
    pairInput: $("pair-input"), pairList: $("pair-listbox"), pairSpinner: $("pair-spinner"),
    pairHelp: $("pair-help"), pairError: $("pair-error"),
    pricePanel: $("price-panel"), priceValue: $("price-value"), priceMeta: $("price-meta"), priceLive: $("price-live"),
    targetInput: $("target-input"), targetError: $("target-error"), targetUnit: $("target-unit"),
    conditionHint: $("condition-hint"),
    createBtn: $("create-btn"), formStatus: $("form-status"),
    alertsList: $("alerts-list"), alertsState: $("alerts-state"), alertsUpdated: $("alerts-updated"),
    refreshBtn: $("refresh-btn"),
    connectDialog: $("connect-dialog"), connectForm: $("connect-form"), keyInput: $("key-input"),
    keyError: $("key-error"), rememberInput: $("remember-input"), apiUrlDisplay: $("api-url-display"),
    disconnectBtn: $("disconnect-btn"), connectSubmit: $("connect-submit"),
    editDialog: $("edit-dialog"), editForm: $("edit-form"), editPair: $("edit-pair"),
    editTarget: $("edit-target"), editError: $("edit-error"), editSubmit: $("edit-submit"),
    confirmDialog: $("confirm-dialog"), confirmTitle: $("confirm-title"),
    confirmMessage: $("confirm-message"), confirmOk: $("confirm-ok"),
    toasts: $("toasts"),
  };

  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value == null) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
    for (const child of [].concat(children)) {
      if (child != null) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function setLoading(button, loading) {
    button.disabled = loading;
    button.classList.toggle("is-loading", loading);
    button.setAttribute("aria-busy", String(loading));
  }

  function toast(message, isError = false) {
    const node = el("div", { class: `toast${isError ? " err" : ""}`, role: isError ? "alert" : "status", text: message });
    els.toasts.append(node);
    setTimeout(() => node.remove(), isError ? 7000 : 4000);
  }

  // ------------------------------------------------------------------ formatting

  function formatPrice(value) {
    if (value == null || value === "") return "—";
    const text = String(value);
    if (!/^\d+(\.\d+)?$/.test(text)) return text;
    let [whole, frac = ""] = text.split(".");
    frac = frac.replace(/0+$/, "").slice(0, 8).padEnd(2, "0");
    whole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return `${whole}.${frac}`;
  }

  const dateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
  const timeFmt = new Intl.DateTimeFormat(undefined, { timeStyle: "medium" });

  function formatDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "—" : dateFmt.format(d);
  }

  function relative(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const minutes = Math.round((Date.now() - d.getTime()) / 60000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} h ago`;
    return `${Math.round(hours / 24)} days ago`;
  }

  /** Compares two non-negative decimal strings exactly. Returns -1, 0 or 1. */
  function compareDecimal(a, b) {
    const [aw, af = ""] = String(a).split(".");
    const [bw, bf = ""] = String(b).split(".");
    const awn = aw.replace(/^0+(?=\d)/, "");
    const bwn = bw.replace(/^0+(?=\d)/, "");
    if (awn.length !== bwn.length) return awn.length > bwn.length ? 1 : -1;
    if (awn !== bwn) return awn > bwn ? 1 : -1;
    const len = Math.max(af.length, bf.length);
    const ap = af.padEnd(len, "0");
    const bp = bf.padEnd(len, "0");
    return ap === bp ? 0 : ap > bp ? 1 : -1;
  }

  function validatePrice(raw) {
    const text = String(raw || "").trim();
    if (!text) return { error: "Enter a target price." };
    if (/,/.test(text)) return { error: "Use a dot (.) as the decimal separator, without thousands separators." };
    if (!/^\d*\.?\d*$/.test(text)) return { error: "Enter a valid positive number." };
    if (!PRICE_RE.test(text)) {
      if (/\.\d{9,}$/.test(text)) return { error: "Use at most 8 decimal places." };
      return { error: "Enter a valid positive number (e.g. 2.5 or 0.00012)." };
    }
    if (compareDecimal(text, "0") <= 0) return { error: "Target price must be greater than zero." };
    return { value: text };
  }

  // ------------------------------------------------------------------ storage (best effort)

  function storageGet(store, key) {
    try { return window[store].getItem(key); } catch { return null; }
  }
  function storageSet(store, key, value) {
    try { window[store].setItem(key, value); } catch { /* storage unavailable */ }
  }
  function storageRemove(store, key) {
    try { window[store].removeItem(key); } catch { /* storage unavailable */ }
  }

  function getAccessKey() {
    return storageGet("sessionStorage", KEY_STORAGE) || storageGet("localStorage", KEY_STORAGE) || "";
  }
  function saveAccessKey(key, remember) {
    clearAccessKey();
    storageSet(remember ? "localStorage" : "sessionStorage", KEY_STORAGE, key);
    if (!getAccessKey()) memoryKey = key; // storage blocked: keep for this page only
  }
  function clearAccessKey() {
    storageRemove("sessionStorage", KEY_STORAGE);
    storageRemove("localStorage", KEY_STORAGE);
    memoryKey = "";
  }
  let memoryKey = "";
  const accessKey = () => getAccessKey() || memoryKey;

  // ------------------------------------------------------------------ network

  async function fetchWithTimeout(url, options = {}, timeoutMs = 12000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(url, { ...options, signal: controller.signal, credentials: "omit", referrerPolicy: "no-referrer" });
    } finally {
      clearTimeout(timer);
    }
  }

  /** GET from Binance's public API, trying each configured host in turn. */
  async function binanceGet(path) {
    let lastError = "unknown error";
    for (const base of CONFIG.binanceBaseUrls) {
      try {
        const res = await fetchWithTimeout(`${base}${path}`, {}, 15000);
        if (res.ok) return { data: await res.json(), host: new URL(base).host };
        if (res.status === 400) {
          const body = await res.json().catch(() => ({}));
          throw Object.assign(new Error(body.msg || "Bad request"), { fatal: true });
        }
        lastError = res.status === 429 || res.status === 418 ? "rate limited by Binance" : `HTTP ${res.status}`;
      } catch (err) {
        if (err.fatal) throw err;
        lastError = err.name === "AbortError" ? "request timed out" : "network error";
      }
    }
    throw new Error(`Could not reach Binance (${lastError}).`);
  }

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }

  async function api(method, path, body) {
    if (!API_CONFIGURED) throw new ApiError("The storage API URL is not configured in config.js.", 0);
    const key = accessKey();
    if (!key) throw new ApiError("Connect with your access key first.", 401);
    let res;
    try {
      res = await fetchWithTimeout(`${API_BASE}${path}`, {
        method,
        headers: {
          Authorization: `Bearer ${key}`,
          ...(body ? { "Content-Type": "application/json" } : {}),
        },
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (err) {
      throw new ApiError(
        err.name === "AbortError" ? "The storage API did not respond in time." : "Could not reach the storage API. Check your connection and the API URL / allowed origin.",
        0
      );
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (res.status === 401) setConnection("error", "Key rejected");
      throw new ApiError(data.error || `Request failed (HTTP ${res.status}).`, res.status);
    }
    return data;
  }

  // ------------------------------------------------------------------ connection state

  function setConnection(state, label) {
    els.connectDot.className = `dot${state === "ok" ? " ok" : state === "error" ? " err" : ""}`;
    els.connectLabel.textContent = label;
    els.connectBtn.setAttribute("aria-label", `Storage connection: ${label}. Open connection settings.`);
  }

  function showSetupBanner() {
    if (API_CONFIGURED) {
      els.banner.hidden = true;
      return;
    }
    els.banner.hidden = false;
    els.banner.replaceChildren(
      el("strong", { text: "Setup needed: " }),
      "set ",
      el("code", { text: "apiBaseUrl" }),
      " in ",
      el("code", { text: "frontend/config.js" }),
      " to your Cloudflare Worker URL (see README, Step 4). You can still browse live prices."
    );
  }

  // ------------------------------------------------------------------ symbol search

  let symbols = []; // [{symbol, base, quote, name, search}]
  let selected = null;
  let options = [];
  let activeIndex = -1;

  function hydrateSymbols(list) {
    symbols = list.map(([symbol, base, quote]) => {
      const name = COIN_NAMES[base] || "";
      return { symbol, base, quote, name, search: `${base} ${quote} ${symbol} ${name}`.toUpperCase() };
    });
  }

  async function loadSymbols() {
    els.pairSpinner.classList.add("visible");
    els.pairInput.disabled = true;
    els.pairError.textContent = "";
    try {
      const cached = JSON.parse(storageGet("localStorage", SYMBOLS_CACHE) || "null");
      if (cached && Date.now() - cached.t < SYMBOLS_TTL_MS && Array.isArray(cached.list) && cached.list.length) {
        hydrateSymbols(cached.list);
      } else {
        const { data } = await binanceGet("/api/v3/exchangeInfo?permissions=SPOT&symbolStatus=TRADING&showPermissionSets=false");
        const list = (data.symbols || [])
          .filter((s) => s.status === "TRADING" && s.isSpotTradingAllowed !== false)
          .map((s) => [s.symbol, s.baseAsset, s.quoteAsset]);
        if (!list.length) throw new Error("Binance returned no trading pairs.");
        hydrateSymbols(list);
        storageSet("localStorage", SYMBOLS_CACHE, JSON.stringify({ t: Date.now(), list }));
      }
      els.pairInput.disabled = false;
      els.pairHelp.textContent = `${symbols.length.toLocaleString()} active Binance Spot pairs. Type a coin or symbol.`;
    } catch (err) {
      els.pairHelp.textContent = "";
      els.pairError.replaceChildren(
        `Could not load trading pairs from Binance: ${err.message} `,
        el("button", { type: "button", class: "btn-link", text: "Try again", onclick: loadSymbols })
      );
    } finally {
      els.pairSpinner.classList.remove("visible");
    }
  }

  function quoteRank(quote) {
    const i = QUOTE_RANK.indexOf(quote);
    return i === -1 ? QUOTE_RANK.length : i;
  }

  function searchSymbols(query) {
    const q = query.toUpperCase().replace(/[\s/\-_]+/g, "");
    const words = query.toUpperCase().split(/[\s/]+/).filter(Boolean);
    if (!q) {
      return POPULAR.map((s) => symbols.find((x) => x.symbol === s)).filter(Boolean);
    }
    const scored = [];
    for (const s of symbols) {
      let score;
      if (s.symbol === q) score = 0;
      else if (s.base === q) score = 1;
      else if (words.length === 2 && s.base === words[0] && s.quote.startsWith(words[1])) score = 1;
      else if (s.symbol.startsWith(q)) score = 2;
      else if (s.name && s.name.toUpperCase().split(" ").some((w) => w.startsWith(q))) score = 3;
      else if (words.every((w) => s.search.includes(w))) score = 4;
      else continue;
      scored.push([score, quoteRank(s.quote), s]);
    }
    scored.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2].symbol.localeCompare(b[2].symbol));
    return scored.slice(0, MAX_RESULTS).map((x) => x[2]);
  }

  function renderOptions() {
    options = searchSymbols(els.pairInput.value);
    activeIndex = options.length ? 0 : -1;
    els.pairList.replaceChildren();
    if (!options.length) {
      els.pairList.append(el("li", { class: "empty", role: "option", "aria-disabled": "true", text: "No matching Binance Spot pairs" }));
    }
    options.forEach((s, i) => {
      els.pairList.append(
        el("li", {
          id: `pair-opt-${i}`, role: "option", "aria-selected": String(i === activeIndex),
          onmousedown: (e) => { e.preventDefault(); choose(s); },
        }, [
          el("span", { class: "pair" }, [s.base, el("span", { class: "quote", text: `/${s.quote}` })]),
          el("span", { class: "name", text: s.name }),
        ])
      );
    });
    openList(true);
    updateActive();
  }

  function openList(open) {
    els.pairList.hidden = !open;
    els.pairInput.setAttribute("aria-expanded", String(open));
    if (!open) els.pairInput.removeAttribute("aria-activedescendant");
  }

  function updateActive() {
    [...els.pairList.children].forEach((li, i) => li.setAttribute("aria-selected", String(i === activeIndex)));
    if (activeIndex >= 0) {
      const li = $(`pair-opt-${activeIndex}`);
      els.pairInput.setAttribute("aria-activedescendant", li.id);
      li.scrollIntoView({ block: "nearest" });
    } else {
      els.pairInput.removeAttribute("aria-activedescendant");
    }
  }

  function choose(s) {
    selected = s;
    els.pairInput.value = `${s.base}/${s.quote}`;
    els.pairInput.removeAttribute("aria-invalid");
    els.pairError.textContent = "";
    els.targetUnit.textContent = `(${s.quote})`;
    openList(false);
    startPricePolling();
    els.targetInput.focus();
  }

  els.pairInput.addEventListener("input", () => {
    if (selected && els.pairInput.value !== `${selected.base}/${selected.quote}`) {
      selected = null;
      els.targetUnit.textContent = "";
      stopPricePolling();
    }
    renderOptions();
  });
  els.pairInput.addEventListener("focus", () => { if (!selected) renderOptions(); });
  els.pairInput.addEventListener("blur", () => openList(false));
  els.pairInput.addEventListener("keydown", (e) => {
    const open = !els.pairList.hidden;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) return renderOptions();
      activeIndex = Math.min(activeIndex + 1, options.length - 1);
      updateActive();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = Math.max(activeIndex - 1, 0);
      updateActive();
    } else if (e.key === "Enter" && open && activeIndex >= 0) {
      e.preventDefault();
      choose(options[activeIndex]);
    } else if (e.key === "Escape" && open) {
      e.preventDefault();
      openList(false);
    }
  });

  // ------------------------------------------------------------------ live price

  let currentPrice = null;
  let priceTimer = null;
  let priceRequest = 0;

  async function refreshPrice() {
    if (!selected || document.hidden) return;
    const symbol = selected.symbol;
    const requestId = ++priceRequest;
    try {
      const { data, host } = await binanceGet(`/api/v3/ticker/price?symbol=${encodeURIComponent(symbol)}`);
      if (requestId !== priceRequest || !selected || selected.symbol !== symbol) return;
      currentPrice = String(data.price);
      els.pricePanel.classList.remove("stale");
      els.priceLive.classList.remove("stale");
      els.priceLive.textContent = "Live";
      els.priceValue.textContent = `${formatPrice(currentPrice)} ${selected.quote}`;
      els.priceMeta.textContent = `${selected.base}/${selected.quote} · Source: Binance (${host}) · Updated ${timeFmt.format(new Date())}`;
    } catch (err) {
      if (requestId !== priceRequest) return;
      els.pricePanel.classList.add("stale");
      els.priceLive.classList.add("stale");
      els.priceLive.textContent = "Stale";
      if (!currentPrice) els.priceValue.textContent = "Unavailable";
      els.priceMeta.textContent = `${err.message} Retrying automatically.`;
    }
    updateConditionHint();
  }

  function startPricePolling() {
    stopPricePolling();
    currentPrice = null;
    els.pricePanel.hidden = false;
    els.priceValue.textContent = "Loading…";
    els.priceMeta.textContent = "";
    refreshPrice();
    priceTimer = setInterval(refreshPrice, Math.max(5, CONFIG.priceRefreshSeconds) * 1000);
  }

  function stopPricePolling() {
    clearInterval(priceTimer);
    priceTimer = null;
    priceRequest++;
    currentPrice = null;
    els.pricePanel.hidden = true;
    updateConditionHint();
  }

  function selectedCondition() {
    return els.form.querySelector('input[name="condition"]:checked').value;
  }

  function updateConditionHint() {
    const { value } = validatePrice(els.targetInput.value);
    let hint = "";
    if (value && currentPrice) {
      const cmp = compareDecimal(currentPrice, value);
      const met = selectedCondition() === "above" ? cmp >= 0 : cmp <= 0;
      if (met) hint = "The current price already meets this condition. The alert will trigger on the next scheduled check.";
    }
    els.conditionHint.textContent = hint;
  }

  els.targetInput.addEventListener("input", () => {
    els.targetError.textContent = "";
    els.targetInput.removeAttribute("aria-invalid");
    updateConditionHint();
  });
  els.form.querySelectorAll('input[name="condition"]').forEach((r) => r.addEventListener("change", updateConditionHint));

  // ------------------------------------------------------------------ create alert

  let creating = false;

  function setFormStatus(message, kind) {
    els.formStatus.textContent = message;
    els.formStatus.className = `form-status${kind ? ` ${kind}` : ""}`;
  }

  els.form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (creating) return; // guard against double submission
    setFormStatus("", "");

    let valid = true;
    if (!selected) {
      els.pairError.textContent = "Choose a trading pair from the list.";
      els.pairInput.setAttribute("aria-invalid", "true");
      valid = false;
    }
    const price = validatePrice(els.targetInput.value);
    if (price.error) {
      els.targetError.textContent = price.error;
      els.targetInput.setAttribute("aria-invalid", "true");
      valid = false;
    }
    if (!valid) {
      (selected ? els.targetInput : els.pairInput).focus();
      return;
    }
    if (!accessKey()) {
      openConnectDialog();
      setFormStatus("Connect with your access key, then create the alert.", "err");
      return;
    }

    creating = true;
    setLoading(els.createBtn, true);
    els.createBtn.querySelector(".btn-label").textContent = "Saving…";
    try {
      const { alert } = await api("POST", "/api/alerts", {
        symbol: selected.symbol,
        target_price: price.value,
        condition: selectedCondition(),
      });
      setFormStatus(`Alert saved: ${pairLabel(alert)} ${alert.condition} ${formatPrice(alert.target_price)}.`, "ok");
      els.targetInput.value = "";
      updateConditionHint();
      upsertAlert(alert);
    } catch (err) {
      setFormStatus(`Could not save the alert: ${err.message}`, "err");
      if (err.status === 401) openConnectDialog();
    } finally {
      creating = false;
      setLoading(els.createBtn, false);
      els.createBtn.querySelector(".btn-label").textContent = "Create alert";
    }
  });

  // ------------------------------------------------------------------ alert list

  let alerts = [];
  let filter = "all";
  let loadingAlerts = false;
  let alertsLoadedOnce = false;
  const busy = new Set();

  function pairLabel(a) {
    return a.base_asset && a.quote_asset ? `${a.base_asset}/${a.quote_asset}` : a.symbol;
  }

  function displayStatus(a) {
    if (a.status === "active") return { label: "Active", cls: "badge-active", group: "active" };
    if (a.status === "cancelled") return { label: "Cancelled", cls: "badge-cancelled", group: "cancelled" };
    if (a.notification_status === "failed") return { label: "Notification Failed", cls: "badge-failed", group: "triggered" };
    return { label: "Triggered", cls: "badge-triggered", group: "triggered" };
  }

  function notificationText(a) {
    const max = CONFIG.maxNotificationAttempts;
    switch (a.notification_status) {
      case "none": return a.status === "active" ? "Waiting for trigger" : "Not sent";
      case "pending": return "Queued, sends on the next check";
      case "sending": return "Sending…";
      case "sent": return `Delivered ${formatDate(a.notified_at)}`;
      case "failed":
        return a.notification_attempts < max
          ? `Failed (attempt ${a.notification_attempts}/${max}), will retry`
          : `Failed after ${a.notification_attempts} attempts`;
      default: return a.notification_status || "—";
    }
  }

  function upsertAlert(alert) {
    const i = alerts.findIndex((a) => a.id === alert.id);
    if (i === -1) alerts.unshift(alert);
    else alerts[i] = alert;
    renderAlerts();
  }

  function renderState(kind, title, detail, action) {
    const box = el("div", { class: `empty-state${kind === "error" ? " err" : ""}` }, [
      el("strong", { text: title }),
      detail ? el("span", { text: detail }) : null,
      action ? el("div", {}, action) : null,
    ]);
    els.alertsState.replaceChildren(box);
  }

  function renderAlerts() {
    const counts = { all: alerts.length, active: 0, triggered: 0, cancelled: 0 };
    alerts.forEach((a) => counts[displayStatus(a).group]++);
    document.querySelectorAll("[data-count]").forEach((n) => { n.textContent = alertsLoadedOnce ? String(counts[n.dataset.count]) : ""; });

    const visible = alerts.filter((a) => filter === "all" || displayStatus(a).group === filter);
    els.alertsList.replaceChildren(...visible.map(renderAlertCard));
    els.alertsState.replaceChildren();
    if (!alertsLoadedOnce) return;
    if (!alerts.length) {
      renderState("empty", "No alerts yet", "Choose a pair, set a target price and create your first alert.");
    } else if (!visible.length) {
      renderState("empty", `No ${filter} alerts`, "");
    }
  }

  function renderAlertCard(a) {
    const status = displayStatus(a);
    const actions = [];
    if (a.status === "active") {
      actions.push(el("button", { type: "button", class: "btn-link", text: "Edit", onclick: () => openEditDialog(a) }));
      actions.push(el("button", { type: "button", class: "btn-link", text: "Cancel alert", onclick: () => confirmAction(a, "cancel") }));
    }
    if (a.notification_status === "failed" && a.status === "triggered") {
      actions.push(el("button", { type: "button", class: "btn-link", text: "Retry notification", onclick: () => mutate(a, "POST", `/api/alerts/${a.id}/retry-notification`, "Notification queued for the next check.") }));
    }
    actions.push(el("button", { type: "button", class: "btn-link danger", text: "Delete", onclick: () => confirmAction(a, "delete") }));

    const condition = a.condition === "above" ? "Above (≥)" : "Below (≤)";
    const observed = a.status === "triggered" ? a.triggered_price : a.last_price;
    const observedAt = a.status === "triggered" ? a.triggered_at : a.last_checked_at;
    const item = (label, value, cls) => el("div", {}, [el("dt", { text: label }), el("dd", { class: cls, text: value })]);

    const notes = [];
    if (a.notification_error && a.notification_status === "failed") {
      notes.push(el("div", { class: "alert-note", text: `Email error: ${a.notification_error}` }));
    }
    if (a.last_error && a.status === "active") {
      notes.push(el("div", { class: "alert-note warn", text: a.last_error }));
    }

    return el("article", { class: `alert-card${busy.has(a.id) ? " is-busy" : ""}`, role: "listitem", "aria-label": `${pairLabel(a)} alert, ${status.label}` }, [
      el("div", { class: "alert-top" }, [
        el("div", { class: "alert-pair" }, [a.base_asset || a.symbol, a.quote_asset ? el("span", { class: "quote", text: `/${a.quote_asset}` }) : null]),
        el("div", { class: "alert-badges" }, [el("span", { class: `badge ${status.cls}`, text: status.label })]),
      ]),
      el("dl", { class: "alert-grid" }, [
        item("Target price", `${formatPrice(a.target_price)} ${a.quote_asset || ""}`.trim(), "mono"),
        item("Condition", condition, a.condition === "above" ? "cond-above" : "cond-below"),
        item(a.status === "triggered" ? "Trigger price" : "Last checked price",
             observed ? `${formatPrice(observed)}${observedAt ? ` · ${relative(observedAt)}` : ""}` : "Not checked yet", "mono"),
        item("Created", formatDate(a.created_at)),
        item("Triggered", a.triggered_at ? formatDate(a.triggered_at) : "—"),
        item("Notification", notificationText(a)),
      ]),
      ...notes,
      el("div", { class: "alert-actions" }, actions),
    ]);
  }

  async function loadAlerts({ quiet = false } = {}) {
    if (loadingAlerts) return;
    if (!API_CONFIGURED) {
      alertsLoadedOnce = false;
      renderAlerts();
      renderState("error", "Storage API not configured", "Set apiBaseUrl in frontend/config.js to your Worker URL.");
      return;
    }
    if (!accessKey()) {
      alertsLoadedOnce = false;
      alerts = [];
      renderAlerts();
      renderState("empty", "Connect to view your alerts", "Your alerts are private. Enter your access key to load them.",
        el("button", { type: "button", class: "btn btn-primary", text: "Connect", onclick: openConnectDialog }));
      setConnection("idle", "Connect");
      return;
    }
    loadingAlerts = true;
    setLoading(els.refreshBtn, true);
    if (!alertsLoadedOnce && !quiet) els.alertsState.replaceChildren(el("div", { class: "empty-state" }, [el("span", { class: "spinner visible", "aria-hidden": "true" }), " Loading alerts…"]));
    try {
      const data = await api("GET", "/api/alerts");
      alerts = Array.isArray(data.alerts) ? data.alerts : [];
      alertsLoadedOnce = true;
      setConnection("ok", "Connected");
      els.alertsUpdated.textContent = `Updated ${timeFmt.format(new Date())}`;
      renderAlerts();
    } catch (err) {
      if (!alertsLoadedOnce) {
        renderState("error", "Could not load alerts", err.message,
          el("button", { type: "button", class: "btn btn-ghost", text: err.status === 401 ? "Update access key" : "Try again", onclick: err.status === 401 ? openConnectDialog : () => loadAlerts() }));
      } else if (!quiet) {
        toast(`Could not refresh alerts: ${err.message}`, true);
      }
      if (err.status !== 401) setConnection("error", "Offline");
    } finally {
      loadingAlerts = false;
      setLoading(els.refreshBtn, false);
    }
  }

  async function mutate(a, method, path, successMessage, body) {
    if (busy.has(a.id)) return false;
    busy.add(a.id);
    renderAlerts();
    try {
      const data = await api(method, path, body);
      if (method === "DELETE") alerts = alerts.filter((x) => x.id !== a.id);
      else if (data.alert) upsertAlert(data.alert);
      toast(successMessage);
      return true;
    } catch (err) {
      toast(err.message, true);
      if (err.status === 409 || err.status === 404) loadAlerts({ quiet: true });
      return false;
    } finally {
      busy.delete(a.id);
      renderAlerts();
    }
  }

  document.querySelectorAll(".chip[data-filter]").forEach((chip) => {
    chip.addEventListener("click", () => {
      filter = chip.dataset.filter;
      document.querySelectorAll(".chip[data-filter]").forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
      renderAlerts();
    });
  });
  els.refreshBtn.addEventListener("click", () => loadAlerts());

  // ------------------------------------------------------------------ dialogs

  document.querySelectorAll("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

  function confirmAction(a, kind) {
    const pair = pairLabel(a);
    const target = `${formatPrice(a.target_price)} (${a.condition})`;
    if (kind === "delete") {
      els.confirmTitle.textContent = "Delete alert?";
      els.confirmMessage.textContent = `The ${pair} alert at ${target} will be permanently deleted. This cannot be undone.`;
      els.confirmOk.textContent = "Delete";
    } else {
      els.confirmTitle.textContent = "Cancel alert?";
      els.confirmMessage.textContent = `The ${pair} alert at ${target} will stop being checked. It stays in your list as Cancelled.`;
      els.confirmOk.textContent = "Cancel alert";
    }
    els.confirmDialog.returnValue = "";
    els.confirmDialog.onclose = () => {
      if (els.confirmDialog.returnValue !== "confirm") return;
      if (kind === "delete") mutate(a, "DELETE", `/api/alerts/${a.id}`, "Alert deleted.");
      else mutate(a, "POST", `/api/alerts/${a.id}/cancel`, "Alert cancelled.");
    };
    els.confirmDialog.showModal();
  }

  let editing = null;

  function openEditDialog(a) {
    editing = a;
    els.editPair.textContent = `${pairLabel(a)} · current target ${formatPrice(a.target_price)} ${a.quote_asset || ""}`;
    els.editTarget.value = a.target_price;
    els.editError.textContent = "";
    els.editTarget.removeAttribute("aria-invalid");
    els.editForm.querySelector(`input[name="edit-condition"][value="${a.condition === "below" ? "below" : "above"}"]`).checked = true;
    els.editDialog.showModal();
  }

  els.editForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!editing) return;
    const price = validatePrice(els.editTarget.value);
    if (price.error) {
      els.editError.textContent = price.error;
      els.editTarget.setAttribute("aria-invalid", "true");
      els.editTarget.focus();
      return;
    }
    const condition = els.editForm.querySelector('input[name="edit-condition"]:checked').value;
    setLoading(els.editSubmit, true);
    const ok = await mutate(editing, "PATCH", `/api/alerts/${editing.id}`, "Alert updated.", {
      target_price: price.value, condition, version: editing.version,
    });
    setLoading(els.editSubmit, false);
    if (ok) els.editDialog.close();
  });

  function openConnectDialog() {
    els.apiUrlDisplay.textContent = API_CONFIGURED ? API_BASE : "(not configured in config.js)";
    els.keyInput.value = "";
    els.keyError.textContent = "";
    els.rememberInput.checked = Boolean(storageGet("localStorage", KEY_STORAGE));
    els.disconnectBtn.hidden = !accessKey();
    if (!els.connectDialog.open) els.connectDialog.showModal();
    els.keyInput.focus();
  }

  els.connectBtn.addEventListener("click", openConnectDialog);

  els.connectForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = els.keyInput.value.trim();
    if (key.length < 16) {
      els.keyError.textContent = "The access key must be at least 16 characters.";
      return;
    }
    const previous = accessKey();
    saveAccessKey(key, els.rememberInput.checked);
    setLoading(els.connectSubmit, true);
    try {
      await api("GET", "/api/alerts");
      els.connectDialog.close();
      toast("Connected to your alert storage.");
      alertsLoadedOnce = false;
      loadAlerts();
    } catch (err) {
      if (previous) saveAccessKey(previous, Boolean(storageGet("localStorage", KEY_STORAGE)));
      else clearAccessKey();
      els.keyError.textContent = err.message;
    } finally {
      setLoading(els.connectSubmit, false);
    }
  });

  els.disconnectBtn.addEventListener("click", () => {
    clearAccessKey();
    els.connectDialog.close();
    toast("Access key removed from this browser.");
    loadAlerts();
  });

  // ------------------------------------------------------------------ startup

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      refreshPrice();
      if (alertsLoadedOnce) loadAlerts({ quiet: true });
    }
  });
  setInterval(() => {
    if (!document.hidden && alertsLoadedOnce) loadAlerts({ quiet: true });
  }, Math.max(15, CONFIG.alertsRefreshSeconds) * 1000);
  setInterval(() => { if (alertsLoadedOnce) renderAlerts(); }, 60000); // keep "x min ago" fresh

  showSetupBanner();
  setConnection("idle", accessKey() ? "Connecting…" : "Connect");
  loadSymbols();
  loadAlerts();
})();
