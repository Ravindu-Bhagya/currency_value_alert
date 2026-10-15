// Public, non-secret configuration for the web app.
// Never put tokens or passwords in this file: it is published on GitHub Pages.
// The access key is typed into the app's "Connect" dialog and kept only in your browser.
window.APP_CONFIG = {
  // URL printed by `npx wrangler deploy` (see README, Step 4). No trailing slash.
  // When the page is served from localhost, the local `wrangler dev` Worker is used instead
  // (override its URL with ?api=http://127.0.0.1:PORT).
  apiBaseUrl: ["localhost", "127.0.0.1"].includes(location.hostname)
    ? new URLSearchParams(location.search).get("api") || "http://127.0.0.1:8787"
    : "https://binance-price-alert.YOUR-SUBDOMAIN.workers.dev",

  // Binance public market-data hosts, tried in order.
  binanceBaseUrls: ["https://data-api.binance.vision", "https://api.binance.com"],

  // How often the selected pair's price and the alert list are refreshed (seconds).
  priceRefreshSeconds: 15,
  alertsRefreshSeconds: 60,

  // Should match MAX_NOTIFICATION_ATTEMPTS used by the checker (display only).
  maxNotificationAttempts: 5,
};
