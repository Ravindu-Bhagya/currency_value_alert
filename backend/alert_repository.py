"""HTTP client for the alert storage API (Cloudflare Worker, see worker/src/index.js).

Uses the CHECKER_TOKEN, which only grants access to the /internal/* endpoints.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from .models import ALERT_ID_PATTERN, Alert, ValidationError

log = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 600


class RepositoryError(Exception):
    pass


@dataclass
class WorkItems:
    active: List[Alert] = field(default_factory=list)
    pending_notifications: List[Alert] = field(default_factory=list)
    invalid_records: int = 0


@dataclass
class Observation:
    alert_id: str
    price: Optional[Decimal]
    error: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.alert_id, "price": None if self.price is None else str(self.price), "error": self.error}


class AlertRepository:
    def __init__(
        self,
        base_url: str,
        token: str,
        session: Optional[requests.Session] = None,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ):
        base_url = (base_url or "").strip().rstrip("/")
        parsed = urlparse(base_url)
        local = parsed.hostname in ("localhost", "127.0.0.1")
        if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
            raise ValueError("ALERTS_API_URL must be an https:// URL.")
        if not token or len(token.strip()) < 16:
            raise ValueError("CHECKER_TOKEN is missing or too short.")
        self.base_url = base_url
        self._headers = {"Authorization": f"Bearer {token.strip()}", "Accept": "application/json"}
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_seconds = backoff_seconds
        self.lease_seconds = lease_seconds
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"AlertRepository(base_url={self.base_url!r}, token=<redacted>)"

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        """Performs a request, retrying network errors, 429 and 5xx. Returns (status, json)."""
        last_problem = "no response"
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.request(
                    method, f"{self.base_url}{path}", json=body, headers=self._headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_problem = type(exc).__name__
            else:
                if resp.status_code < 500 and resp.status_code != 429:
                    try:
                        data = resp.json()
                    except ValueError:
                        data = {}
                    if resp.status_code in (401, 403):
                        raise RepositoryError("Alert API rejected the CHECKER_TOKEN (HTTP %d)." % resp.status_code)
                    return resp.status_code, data
                last_problem = f"HTTP {resp.status_code}"
            log.warning("Alert API %s %s failed (%s), attempt %d/%d", method, path, last_problem, attempt, self.max_retries)
            if attempt < self.max_retries:
                self._sleep(self.backoff_seconds * (2 ** (attempt - 1)))
        raise RepositoryError(f"Alert API unavailable: {method} {path} ({last_problem}).")

    @staticmethod
    def _check_id(alert_id: str) -> str:
        if not ALERT_ID_PATTERN.match(alert_id):
            raise ValidationError(f"Invalid alert id: {alert_id!r}")
        return alert_id

    @staticmethod
    def _error(status: int, data: Any) -> str:
        message = data.get("error") if isinstance(data, dict) else None
        return f"HTTP {status}: {message or 'unexpected response'}"

    def _parse_alerts(self, records: Iterable[Any], work: WorkItems) -> List[Alert]:
        alerts = []
        for record in records or []:
            try:
                alerts.append(Alert.from_dict(record))
            except ValidationError as exc:
                work.invalid_records += 1
                log.error("Skipping invalid alert record: %s", exc)
        return alerts

    def fetch_work(self) -> WorkItems:
        status, data = self._request("GET", "/internal/work")
        if status != 200 or not isinstance(data, dict):
            raise RepositoryError(f"Could not load alerts: {self._error(status, data)}")
        work = WorkItems()
        work.active = self._parse_alerts(data.get("active"), work)
        work.pending_notifications = self._parse_alerts(data.get("pending_notifications"), work)
        return work

    def record_observations(self, observations: List[Observation]) -> None:
        for start in range(0, len(observations), 500):
            chunk = [o.to_json() for o in observations[start:start + 500]]
            status, data = self._request("POST", "/internal/observations", {"observations": chunk})
            if status != 200:
                raise RepositoryError(f"Could not record prices: {self._error(status, data)}")

    def claim_trigger(self, alert: Alert, price: Decimal) -> Optional[Alert]:
        """Atomically marks an active alert as triggered. Returns None if another run or a user edit won."""
        path = f"/internal/alerts/{self._check_id(alert.id)}/trigger"
        status, data = self._request(
            "POST", path, {"version": alert.version, "price": str(price), "lease_seconds": self.lease_seconds}
        )
        return self._claim_result(status, data)

    def claim_notification(self, alert_id: str, max_attempts: int) -> Optional[Alert]:
        """Takes a time-limited lease on a pending/failed notification. Returns None if not claimable."""
        path = f"/internal/alerts/{self._check_id(alert_id)}/notification/claim"
        status, data = self._request("POST", path, {"max_attempts": max_attempts, "lease_seconds": self.lease_seconds})
        return self._claim_result(status, data)

    def _claim_result(self, status: int, data: Any) -> Optional[Alert]:
        if status == 409:
            return None
        if status != 200 or not isinstance(data, dict) or not data.get("claimed"):
            raise RepositoryError(f"Claim failed: {self._error(status, data)}")
        return Alert.from_dict(data["alert"])

    def record_notification_result(self, alert_id: str, delivered: bool, error: Optional[str] = None) -> None:
        path = f"/internal/alerts/{self._check_id(alert_id)}/notification/result"
        body = {"result": "sent" if delivered else "failed", "error": (error or "")[:300] or None}
        status, data = self._request("POST", path, body)
        if status != 200:
            raise RepositoryError(f"Could not record notification result: {self._error(status, data)}")
