import logging

import pytest
import requests

from backend.check_prices import SecretRedactingFilter
from backend.telegram_notifier import TelegramError, TelegramNotifier
from tests.conftest import TELEGRAM_TOKEN, FakeResponse, FakeSession

OK = FakeResponse(200, {"ok": True, "result": {"message_id": 42}})


def notifier(responses):
    sleeps = []
    session = FakeSession(responses)
    return TelegramNotifier(TELEGRAM_TOKEN, "123456", session=session, sleep=sleeps.append), session, sleeps


def test_sends_via_official_api():
    n, session, _ = notifier([OK])
    assert n.send_message("hello") == 42
    call = session.calls[0]
    assert call["url"] == f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    assert call["json"]["chat_id"] == "123456"
    assert call["json"]["text"] == "hello"
    assert call["timeout"] == 15.0


@pytest.mark.parametrize("token,chat", [("", "1"), ("not-a-token", "1"), (TELEGRAM_TOKEN, ""), (TELEGRAM_TOKEN, "abc")])
def test_rejects_bad_configuration_without_echoing_values(token, chat):
    with pytest.raises(ValueError) as exc:
        TelegramNotifier(token, chat)
    assert TELEGRAM_TOKEN not in str(exc.value)


def test_retries_server_errors():
    n, session, sleeps = notifier([FakeResponse(502, {"ok": False, "description": "Bad Gateway"}), OK])
    n.send_message("hi")
    assert len(session.calls) == 2
    assert sleeps == [2.0]


def test_retries_connection_errors():
    n, session, _ = notifier([requests.ConnectionError("down"), OK])
    n.send_message("hi")
    assert len(session.calls) == 2


def test_rate_limit_uses_retry_after():
    n, _, sleeps = notifier([FakeResponse(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}}), OK])
    n.send_message("hi")
    assert sleeps == [3.0]


def test_read_timeout_is_not_retried_immediately():
    # The message may already have been delivered; avoid an instant duplicate.
    n, session, _ = notifier([requests.ReadTimeout("slow")])
    with pytest.raises(TelegramError):
        n.send_message("hi")
    assert len(session.calls) == 1


def test_permanent_errors_are_not_retried():
    n, session, _ = notifier([FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"})])
    with pytest.raises(TelegramError) as exc:
        n.send_message("hi")
    assert exc.value.permanent
    assert "chat not found" in str(exc.value)
    assert len(session.calls) == 1


def test_gives_up_after_max_retries():
    n, session, _ = notifier([FakeResponse(500, {"ok": False})] * 3)
    with pytest.raises(TelegramError) as exc:
        n.send_message("hi")
    assert not exc.value.permanent
    assert len(session.calls) == 3


def test_token_is_redacted_from_errors_and_repr():
    n, _, _ = notifier([FakeResponse(401, {"ok": False, "description": f"Unauthorized {TELEGRAM_TOKEN}"})])
    with pytest.raises(TelegramError) as exc:
        n.send_message("hi")
    assert TELEGRAM_TOKEN not in str(exc.value)
    assert TELEGRAM_TOKEN not in repr(n)


def test_log_filter_redacts_secrets():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "url=%s", (f"/bot{TELEGRAM_TOKEN}/send",), None)
    SecretRedactingFilter([TELEGRAM_TOKEN]).filter(record)
    assert TELEGRAM_TOKEN not in record.getMessage()


def test_find_chat_ids():
    updates = {"ok": True, "result": [{"message": {"chat": {"id": 987, "first_name": "Sam"}}}]}
    n, _, _ = notifier([FakeResponse(200, updates)])
    assert n.find_chat_ids() == [("987", "Sam")]
