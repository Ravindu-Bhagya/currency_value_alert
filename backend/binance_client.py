"""Minimal read-only client for the Binance public Spot market-data API.

No API key is used or needed. This client never places orders.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, Optional, Sequence

import requests

from .models import normalize_symbol

log = logging.getLogger(__name__)

# data-api.binance.vision is Binance's dedicated public market-data host. It is tried first
# because api.binance.com answers HTTP 451 to some cloud regions (including some GitHub
# Actions runners). Both serve identical /api/v3 market-data endpoints.
DEFAULT_BASE_URLS: Sequence[str] = ("https://data-api.binance.vision", "https://api.binance.com")
MAX_RETRY_AFTER_SECONDS = 60


class BinanceError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class InvalidSymbolError(BinanceError):
    pass


class BinanceClient:
    def __init__(
        self,
        base_urls: Optional[Iterable[str]] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_urls = [u.rstrip("/") for u in (base_urls or DEFAULT_BASE_URLS) if u.strip()]
        if not self.base_urls:
            raise ValueError("At least one Binance base URL is required.")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep

    def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Any:
        last_problem = "no response"
        for base in self.base_urls:
            for attempt in range(1, self.max_retries + 1):
                url = f"{base}{path}"
                try:
                    resp = self.session.get(url, params=params, timeout=self.timeout)
                except requests.RequestException as exc:
                    last_problem = f"{base}: {type(exc).__name__}"
                    log.warning("Binance request failed (%s), attempt %d/%d", last_problem, attempt, self.max_retries)
                    self._backoff(attempt)
                    continue

                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        raise BinanceError("Binance returned invalid JSON.", 200) from None

                if resp.status_code in (403, 451):
                    # Region restriction / WAF block for this host: try the next host.
                    last_problem = f"{base}: HTTP {resp.status_code} (restricted)"
                    log.warning("Binance host unavailable: %s", last_problem)
                    break
                if resp.status_code == 418:
                    raise BinanceError("Binance has temporarily banned this IP (HTTP 418).", 418)
                if resp.status_code == 429:
                    wait = _retry_after(resp, self.backoff_seconds * attempt)
                    last_problem = f"{base}: HTTP 429 rate limited"
                    log.warning("Binance rate limit hit; waiting %.1fs", wait)
                    if attempt < self.max_retries:
                        self._sleep(wait)
                    continue
                if resp.status_code >= 500:
                    last_problem = f"{base}: HTTP {resp.status_code}"
                    log.warning("Binance server error (%s), attempt %d/%d", last_problem, attempt, self.max_retries)
                    self._backoff(attempt)
                    continue

                code, msg = _error_body(resp)
                if code == -1121:
                    raise InvalidSymbolError(f"Invalid symbol: {msg}", resp.status_code, code)
                raise BinanceError(f"Binance rejected the request: HTTP {resp.status_code} {msg}", resp.status_code, code)
        raise BinanceError(f"Binance API unavailable ({last_problem}).")

    def _backoff(self, attempt: int) -> None:
        if attempt < self.max_retries:
            self._sleep(self.backoff_seconds * (2 ** (attempt - 1)))

    def get_all_prices(self) -> Dict[str, Decimal]:
        """Latest price of every symbol in one request (request weight 4)."""
        data = self._get("/api/v3/ticker/price")
        if not isinstance(data, list):
            raise BinanceError("Unexpected response format for ticker/price.")
        prices: Dict[str, Decimal] = {}
        for item in data:
            try:
                price = Decimal(str(item["price"]))
            except (KeyError, TypeError, InvalidOperation):
                continue
            if price.is_finite() and price > 0:
                prices[str(item.get("symbol"))] = price
        return prices

    def get_prices(self, symbols: Iterable[str]) -> Dict[str, Decimal]:
        """Prices for the requested symbols; symbols Binance does not list are omitted."""
        wanted = {normalize_symbol(s) for s in symbols}
        if not wanted:
            return {}
        all_prices = self.get_all_prices()
        return {s: all_prices[s] for s in wanted if s in all_prices}

    def get_price(self, symbol: str) -> Decimal:
        symbol = normalize_symbol(symbol)
        data = self._get("/api/v3/ticker/price", {"symbol": symbol})
        try:
            return Decimal(str(data["price"]))
        except (KeyError, TypeError, InvalidOperation):
            raise BinanceError("Unexpected response format for ticker/price.") from None


def _retry_after(resp: requests.Response, default: float) -> float:
    try:
        value = float(resp.headers.get("Retry-After", default))
    except (TypeError, ValueError):
        value = default
    return max(0.0, min(value, MAX_RETRY_AFTER_SECONDS))


def _error_body(resp: requests.Response) -> tuple:
    try:
        body = resp.json()
        return body.get("code"), str(body.get("msg", ""))[:200]
    except (ValueError, AttributeError):
        return None, resp.text[:200] if isinstance(resp.text, str) else ""
