-- Binance Price Alert - Cloudflare D1 schema.
-- Apply with: npx wrangler d1 execute binance-alerts --remote --file=schema.sql

CREATE TABLE IF NOT EXISTS alerts (
  id                        TEXT PRIMARY KEY,
  symbol                    TEXT NOT NULL,
  base_asset                TEXT NOT NULL,
  quote_asset               TEXT NOT NULL,
  -- Prices are stored as normalised decimal strings to avoid float rounding.
  target_price              TEXT NOT NULL,
  condition                 TEXT NOT NULL CHECK (condition IN ('above', 'below')),
  status                    TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'triggered', 'cancelled')),
  notification_status       TEXT NOT NULL DEFAULT 'none'
                              CHECK (notification_status IN ('none', 'pending', 'sending', 'sent', 'failed')),
  notification_attempts     INTEGER NOT NULL DEFAULT 0,
  notification_error        TEXT,
  notification_lease_until  TEXT,
  last_price                TEXT,
  last_checked_at           TEXT,
  last_error                TEXT,
  triggered_price           TEXT,
  triggered_at              TEXT,
  notified_at               TEXT,
  created_at                TEXT NOT NULL,
  updated_at                TEXT NOT NULL,
  -- Incremented on every change; used for optimistic concurrency control.
  version                   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (status, notification_status);

-- Prevents duplicate active alerts (e.g. a double-submitted form).
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_unique_active
  ON alerts (symbol, target_price, condition)
  WHERE status = 'active';
