"""Shared test helpers. No test performs real network calls."""

from __future__ import annotations

import copy
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest
import requests

from backend.alert_repository import Observation, RepositoryError, WorkItems
from backend.models import Alert

class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: Any = None, headers: Optional[dict] = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Returns queued responses (or raises queued exceptions) and records every call."""

    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def _next(self, **call):
        self.calls.append(call)
        if not self.responses:
            raise AssertionError("Unexpected extra HTTP request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        return self._next(method="GET", url=url, **kwargs)

    def post(self, url, **kwargs):
        return self._next(method="POST", url=url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._next(method=method, url=url, **kwargs)


def make_record(**overrides) -> Dict[str, Any]:
    record = {
        "id": str(uuid.uuid4()),
        "symbol": "XRPUSDT",
        "base_asset": "XRP",
        "quote_asset": "USDT",
        "target_price": "2.5",
        "condition": "above",
        "status": "active",
        "notification_status": "none",
        "notification_attempts": 0,
        "version": 1,
        "created_at": "2026-10-15T00:00:00.000Z",
    }
    record.update(overrides)
    return record


class InMemoryRepository:
    """Mirrors the conditional-update semantics of the Worker API, in memory."""

    def __init__(self, records: Optional[List[Dict[str, Any]]] = None):
        self.records = {r["id"]: dict(r) for r in (records or [])}
        self.observations: List[Observation] = []
        self.fail_fetch = False
        self.fail_results = False

    def add(self, **overrides) -> Dict[str, Any]:
        record = make_record(**overrides)
        self.records[record["id"]] = record
        return record

    def fetch_work(self) -> WorkItems:
        if self.fail_fetch:
            raise RepositoryError("storage down")
        work = WorkItems()
        for r in self.records.values():
            if r["status"] == "active":
                work.active.append(Alert.from_dict(copy.deepcopy(r)))
            elif r["status"] == "triggered" and r["notification_status"] in ("pending", "failed"):
                work.pending_notifications.append(Alert.from_dict(copy.deepcopy(r)))
        return work

    def record_observations(self, observations: List[Observation]) -> None:
        self.observations.extend(observations)
        for o in observations:
            r = self.records.get(o.alert_id)
            if r and r["status"] == "active" and o.price is not None:
                r["last_price"] = str(o.price)

    def claim_trigger(self, alert: Alert, price: Decimal) -> Optional[Alert]:
        r = self.records.get(alert.id)
        if not r or r["status"] != "active" or r["version"] != alert.version:
            return None
        r.update(status="triggered", triggered_price=str(price), last_price=str(price),
                 notification_status="sending", notification_attempts=1, version=r["version"] + 1)
        return Alert.from_dict(copy.deepcopy(r))

    def claim_notification(self, alert_id: str, max_attempts: int) -> Optional[Alert]:
        r = self.records.get(alert_id)
        if (not r or r["status"] != "triggered" or r["notification_status"] not in ("pending", "failed")
                or r["notification_attempts"] >= max_attempts):
            return None
        r.update(notification_status="sending", notification_attempts=r["notification_attempts"] + 1,
                 version=r["version"] + 1)
        return Alert.from_dict(copy.deepcopy(r))

    def record_notification_result(self, alert_id: str, delivered: bool, error: Optional[str] = None) -> None:
        if self.fail_results:
            raise RepositoryError("storage down")
        r = self.records[alert_id]
        assert r["notification_status"] == "sending"
        r["notification_status"] = "sent" if delivered else "failed"
        r["notification_error"] = error


class FakeBinance:
    def __init__(self, prices: Optional[Dict[str, str]] = None, error: Optional[Exception] = None):
        self.prices = {k: Decimal(v) for k, v in (prices or {}).items()}
        self.error = error
        self.calls = 0

    def get_prices(self, symbols):
        self.calls += 1
        if self.error:
            raise self.error
        return {s: self.prices[s] for s in symbols if s in self.prices}


class FakeNotifier:
    def __init__(self, fail_times: int = 0, error: Optional[Exception] = None):
        self.sent: List[str] = []
        self.fail_times = fail_times
        self.error = error

    def send_message(self, text: str, subject: Optional[str] = None) -> int:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        self.sent.append(text)
        return len(self.sent)


@pytest.fixture
def no_sleep():
    sleeps: List[float] = []
    return sleeps.append, sleeps


__all__ = ["FakeResponse", "FakeSession", "make_record", "InMemoryRepository", "FakeBinance", "FakeNotifier", "requests"]
