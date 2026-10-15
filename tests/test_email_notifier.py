import logging
import smtplib
import socket

import pytest

from backend.check_prices import SecretRedactingFilter
from backend.email_notifier import EmailNotifier, NotificationError, parse_recipients

PASSWORD = "abcd efgh ijkl mnop"


class FakeSMTP:
    """Records SMTP calls; each connection pops the next queued outcome (None = success)."""

    outcomes = []
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls = []
        FakeSMTP.instances.append(self)
        outcome = FakeSMTP.outcomes.pop(0) if FakeSMTP.outcomes else None
        self.connect_error = outcome if isinstance(outcome, OSError) else None
        self.send_error = outcome if outcome is not None and self.connect_error is None else None
        if self.connect_error:
            raise self.connect_error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if isinstance(self.send_error, smtplib.SMTPAuthenticationError):
            raise self.send_error

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.calls.append(("send", msg, from_addr, to_addrs))
        if self.send_error:
            raise self.send_error


@pytest.fixture
def smtp():
    FakeSMTP.outcomes, FakeSMTP.instances = [], []
    return FakeSMTP


def notifier(port=587, to=("me@example.com",)):
    sleeps = []
    n = EmailNotifier("smtp.example.com", port, "bot@example.com", PASSWORD, "", list(to),
                      sleep=sleeps.append, smtp_factory=FakeSMTP)
    return n, sleeps


def test_sends_with_starttls_login_and_subject(smtp):
    n, _ = notifier()
    message_id = n.send_message("Price reached", subject="BTC/USDT is above 1.00")
    server = smtp.instances[0]
    assert (server.host, server.port, server.timeout) == ("smtp.example.com", 587, 20.0)
    assert server.calls[0] == "starttls"
    assert server.calls[1] == ("login", "bot@example.com", PASSWORD)
    _, msg, from_addr, to_addrs = server.calls[2]
    assert msg["Subject"] == "BTC/USDT is above 1.00"
    assert msg["From"] == "bot@example.com"          # EMAIL_FROM defaults to the username
    assert msg["To"] == "me@example.com"
    assert msg.get_content().strip() == "Price reached"
    assert (from_addr, to_addrs) == ("bot@example.com", ["me@example.com"])
    assert message_id == msg["Message-ID"]


def test_port_465_uses_implicit_tls_without_starttls(smtp):
    n, _ = notifier(port=465)
    n.send_message("x")
    server = smtp.instances[0]
    assert server.context is not None
    assert "starttls" not in server.calls


def test_multiple_recipients(smtp):
    n, _ = notifier(to=parse_recipients("a@example.com, b@example.com;c@example.com"))
    n.send_message("x")
    assert smtp.instances[0].calls[-1][3] == ["a@example.com", "b@example.com", "c@example.com"]


@pytest.mark.parametrize("host,port,user,pw,frm,to", [
    ("", 587, "u@example.com", PASSWORD, "", ["me@example.com"]),
    ("h", 0, "u@example.com", PASSWORD, "", ["me@example.com"]),
    ("h", 587, "", PASSWORD, "", ["me@example.com"]),
    ("h", 587, "u@example.com", "", "", ["me@example.com"]),
    ("h", 587, "u@example.com", PASSWORD, "not-an-email", ["me@example.com"]),
    ("h", 587, "u@example.com", PASSWORD, "", []),
    ("h", 587, "u@example.com", PASSWORD, "", ["nope"]),
])
def test_rejects_bad_configuration_without_echoing_password(host, port, user, pw, frm, to):
    with pytest.raises(ValueError) as exc:
        EmailNotifier(host, port, user, pw, frm, to)
    assert PASSWORD not in str(exc.value)


def test_connection_errors_are_retried_with_backoff(smtp):
    smtp.outcomes = [ConnectionRefusedError(), smtplib.SMTPServerDisconnected("bye"), None]
    n, sleeps = notifier()
    n.send_message("x")
    assert len(smtp.instances) == 3
    assert sleeps == [2.0, 4.0]


def test_gives_up_after_max_retries(smtp):
    smtp.outcomes = [ConnectionRefusedError()] * 3
    n, _ = notifier()
    with pytest.raises(NotificationError) as exc:
        n.send_message("x")
    assert not exc.value.permanent
    assert len(smtp.instances) == 3


def test_temporary_4xx_is_retried(smtp):
    smtp.outcomes = [smtplib.SMTPDataError(451, b"try later"), None]
    n, _ = notifier()
    n.send_message("x")
    assert len(smtp.instances) == 2


def test_timeout_is_not_retried_to_avoid_duplicates(smtp):
    smtp.outcomes = [socket.timeout()]
    n, _ = notifier()
    with pytest.raises(NotificationError):
        n.send_message("x")
    assert len(smtp.instances) == 1


def test_bad_login_is_permanent_and_redacted(smtp):
    smtp.outcomes = [smtplib.SMTPAuthenticationError(535, f"bad {PASSWORD}".encode())]
    n, _ = notifier()
    with pytest.raises(NotificationError) as exc:
        n.send_message("x")
    assert exc.value.permanent and exc.value.status_code == 535
    assert PASSWORD not in str(exc.value)
    assert PASSWORD not in repr(n)
    assert len(smtp.instances) == 1


def test_refused_recipient_is_permanent(smtp):
    smtp.outcomes = [smtplib.SMTPRecipientsRefused({"me@example.com": (550, b"no such user")})]
    n, _ = notifier()
    with pytest.raises(NotificationError) as exc:
        n.send_message("x")
    assert exc.value.permanent and exc.value.status_code == 550


def test_log_filter_redacts_password():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "pw=%s", (PASSWORD,), None)
    SecretRedactingFilter([PASSWORD]).filter(record)
    assert PASSWORD not in record.getMessage()
