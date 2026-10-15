from decimal import Decimal

import pytest
import requests

from backend.alert_repository import AlertRepository, Observation, RepositoryError
from backend.models import Alert
from tests.conftest import FakeResponse, FakeSession, make_record

TOKEN = "checker-token-0123456789abcdef"


def repo(responses):
    sleeps = []
    session = FakeSession(responses)
    return AlertRepository("https://alerts.example.workers.dev/", TOKEN, session=session, sleep=sleeps.append), session


def test_requires_https_and_token():
    with pytest.raises(ValueError):
        AlertRepository("http://alerts.example.com", TOKEN)
    with pytest.raises(ValueError):
        AlertRepository("https://alerts.example.com", "short")
    AlertRepository("http://localhost:8787", TOKEN)  # local wrangler dev is allowed


def test_fetch_work_parses_and_skips_invalid_records():
    good = make_record()
    pending = make_record(status="triggered", notification_status="failed", notification_attempts=1, triggered_price="3")
    r, session = repo([FakeResponse(200, {"active": [good, {"id": "bad"}], "pending_notifications": [pending]})])
    work = r.fetch_work()
    assert [a.id for a in work.active] == [good["id"]]
    assert [a.id for a in work.pending_notifications] == [pending["id"]]
    assert work.invalid_records == 1
    assert session.calls[0]["url"] == "https://alerts.example.workers.dev/internal/work"
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_auth_failure_raises_without_retry():
    r, session = repo([FakeResponse(401, {"error": "Invalid"})])
    with pytest.raises(RepositoryError):
        r.fetch_work()
    assert len(session.calls) == 1


def test_retries_transient_errors():
    r, session = repo([requests.ConnectionError(), FakeResponse(503), FakeResponse(200, {"active": [], "pending_notifications": []})])
    assert r.fetch_work().active == []
    assert len(session.calls) == 3


def test_unavailable_after_retries():
    r, _ = repo([FakeResponse(500)] * 3)
    with pytest.raises(RepositoryError):
        r.fetch_work()


def test_claim_trigger_success_and_conflict():
    record = make_record()
    alert = Alert.from_dict(record)
    claimed = dict(record, status="triggered", notification_status="sending", version=2, triggered_price="2.6")
    r, session = repo([FakeResponse(200, {"claimed": True, "alert": claimed}), FakeResponse(409, {"claimed": False})])
    result = r.claim_trigger(alert, Decimal("2.6"))
    assert result.status.value == "triggered"
    assert session.calls[0]["json"]["version"] == 1
    assert session.calls[0]["json"]["price"] == "2.6"
    assert r.claim_trigger(alert, Decimal("2.6")) is None


def test_record_observations_and_result():
    record = make_record()
    r, session = repo([FakeResponse(200, {"updated": 1}), FakeResponse(200, {"alert": record})])
    r.record_observations([Observation(record["id"], Decimal("2.4"))])
    assert session.calls[0]["json"] == {"observations": [{"id": record["id"], "price": "2.4", "error": None}]}
    r.record_notification_result(record["id"], False, "HTTP 502")
    assert session.calls[1]["json"] == {"result": "failed", "error": "HTTP 502"}


def test_rejects_malformed_alert_id_before_request():
    r, session = repo([])
    with pytest.raises(ValueError):
        r.record_notification_result("../../admin", True)
    assert session.calls == []
