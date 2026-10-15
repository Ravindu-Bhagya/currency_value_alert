import logging

import pytest
import requests

from backend.check_prices import SecretRedactingFilter
from backend.whatsapp_notifier import NotificationError, WhatsAppNotifier
from tests.conftest import FakeResponse, FakeSession

SID = "AC0123456789abcdef0123456789abcdef"
AUTH = "test_auth_token_abcdef0123456789"
OK = FakeResponse(201, {"sid": "SM123", "status": "queued"})


def notifier(responses):
    sleeps = []
    session = FakeSession(responses)
    n = WhatsAppNotifier(SID, AUTH, "whatsapp:+14155238886", "+94705049478", session=session, sleep=sleeps.append)
    return n, session, sleeps


def test_sends_via_twilio_messages_api():
    n, session, _ = notifier([OK])
    assert n.send_message("hello") == "SM123"
    call = session.calls[0]
    assert call["url"] == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json"
    assert call["data"] == {"From": "whatsapp:+14155238886", "To": "whatsapp:+94705049478", "Body": "hello"}
    assert call["auth"] == (SID, AUTH)
    assert call["timeout"] == 15.0


@pytest.mark.parametrize("raw", ["+94705049478", "94705049478", "whatsapp:+94705049478", " WhatsApp:+94705049478 "])
def test_normalizes_destination_number(raw):
    n = WhatsAppNotifier(SID, AUTH, "+14155238886", raw, session=FakeSession([]))
    assert n.to_number == "whatsapp:+94705049478"
    assert n.from_number == "whatsapp:+14155238886"


@pytest.mark.parametrize("sid,token,frm,to", [
    ("", AUTH, "+14155238886", "+94705049478"),
    (SID, "", "+14155238886", "+94705049478"),
    (SID, AUTH, "", "+94705049478"),
    (SID, AUTH, "+14155238886", "not-a-number"),
    (SID, AUTH, "+14155238886", "+0123"),
])
def test_rejects_bad_configuration_without_echoing_values(sid, token, frm, to):
    with pytest.raises(ValueError) as exc:
        WhatsAppNotifier(sid, token, frm, to)
    assert AUTH not in str(exc.value)


def test_retries_server_errors_with_backoff():
    n, session, sleeps = notifier([FakeResponse(500, {"message": "oops"}), FakeResponse(503, {}), OK])
    assert n.send_message("x") == "SM123"
    assert len(session.calls) == 3
    assert sleeps == [2.0, 4.0]


def test_gives_up_after_max_retries():
    n, _, _ = notifier([FakeResponse(500, {})] * 3)
    with pytest.raises(NotificationError) as exc:
        n.send_message("x")
    assert not exc.value.permanent


def test_connection_error_is_retried():
    n, session, _ = notifier([requests.ConnectionError("down"), OK])
    assert n.send_message("x") == "SM123"
    assert len(session.calls) == 2


def test_read_timeout_is_not_retried_to_avoid_duplicates():
    n, session, _ = notifier([requests.ReadTimeout("slow"), OK])
    with pytest.raises(NotificationError):
        n.send_message("x")
    assert len(session.calls) == 1


def test_rate_limit_uses_retry_after_header_not_error_code():
    limited = FakeResponse(429, {"code": 20429, "message": "Too Many Requests"}, headers={"Retry-After": "3"})
    n, _, sleeps = notifier([limited, OK])
    assert n.send_message("x") == "SM123"
    assert sleeps == [3.0]


def test_rate_limit_with_long_retry_after_gives_up():
    limited = FakeResponse(429, {"message": "slow down"}, headers={"Retry-After": "3600"})
    n, session, _ = notifier([limited])
    with pytest.raises(NotificationError):
        n.send_message("x")
    assert len(session.calls) == 1


def test_client_errors_are_permanent_and_redacted():
    n, _, _ = notifier([FakeResponse(401, {"code": 20003, "message": f"Authenticate {AUTH}"})])
    with pytest.raises(NotificationError) as exc:
        n.send_message("x")
    assert exc.value.permanent
    assert exc.value.status_code == 401
    assert AUTH not in str(exc.value)
    assert AUTH not in repr(n) and SID not in repr(n)


def test_log_filter_redacts_secrets():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "auth=%s", (AUTH,), None)
    SecretRedactingFilter([AUTH]).filter(record)
    assert AUTH not in record.getMessage()
