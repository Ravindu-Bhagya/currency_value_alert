"""Sends Binance alert messages by email over SMTP (e.g. Gmail with an App Password)."""

from __future__ import annotations

import logging
import re
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
DEFAULT_SUBJECT = "Binance Price Alert"


class NotificationError(Exception):
    """An email could not be delivered.

    permanent=True means retrying will not help without a configuration change
    (e.g. wrong password or a rejected address). It is still retried on later runs up
    to the attempt limit, in case the configuration is fixed in the meantime.
    """

    def __init__(self, message: str, permanent: bool = False, status_code: Optional[int] = None):
        super().__init__(message)
        self.permanent = permanent
        self.status_code = status_code


def parse_recipients(value: str) -> List[str]:
    return [part.strip() for part in re.split(r"[,;]", value or "") if part.strip()]


class EmailNotifier:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        from_addr: str,
        to_addrs: List[str],
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        smtp_factory: Optional[Callable[..., smtplib.SMTP]] = None,
    ):
        self.host = (host or "").strip()
        self.port = int(port)
        self.username = (username or "").strip()
        self._password = password or ""
        self.from_addr = (from_addr or "").strip() or self.username
        self.to_addrs = [a.strip() for a in to_addrs if a and a.strip()]
        # Error messages deliberately never include the password.
        if not self.host:
            raise ValueError("SMTP_HOST is missing.")
        if not 0 < self.port < 65536:
            raise ValueError("SMTP_PORT is invalid.")
        if not self.username or not self._password:
            raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must both be set.")
        if not EMAIL_RE.match(self.from_addr):
            raise ValueError("EMAIL_FROM is missing or malformed.")
        if not self.to_addrs or not all(EMAIL_RE.match(a) for a in self.to_addrs):
            raise ValueError("EMAIL_TO is missing or malformed.")
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep
        # Port 465 is implicit TLS; any other port (587, 25) upgrades with STARTTLS.
        self._implicit_tls = self.port == 465
        self._smtp_factory = smtp_factory or (smtplib.SMTP_SSL if self._implicit_tls else smtplib.SMTP)

    def __repr__(self) -> str:  # never leak the password through logging/repr
        return f"EmailNotifier(host={self.host!r}, port={self.port}, password=<redacted>)"

    def _redact(self, text: str) -> str:
        return text.replace(self._password, "<redacted>") if len(self._password) >= 4 else text

    def build_message(self, text: str, subject: Optional[str] = None) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = subject or DEFAULT_SUBJECT
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.from_addr.split("@")[-1])
        msg.set_content(text)
        return msg

    def send_message(self, text: str, subject: Optional[str] = None) -> str:
        """Sends a plain-text email. Returns its Message-ID or raises NotificationError."""
        msg = self.build_message(text, subject)
        last_problem = "no response"
        for attempt in range(1, self.max_retries + 1):
            try:
                self._send_once(msg)
                return str(msg["Message-ID"])
            except smtplib.SMTPAuthenticationError as exc:
                raise NotificationError(
                    f"SMTP login failed ({exc.smtp_code}). Check SMTP_USERNAME / SMTP_PASSWORD "
                    "(Gmail needs an App Password).", permanent=True, status_code=exc.smtp_code) from None
            except smtplib.SMTPRecipientsRefused as exc:
                codes = [code for code, _ in exc.recipients.values()]
                if all(400 <= c < 500 for c in codes):
                    last_problem = f"recipient temporarily refused ({codes})"
                else:
                    raise NotificationError(f"The mail server refused EMAIL_TO ({codes}).",
                                            permanent=True, status_code=codes[0] if codes else None) from None
            except smtplib.SMTPResponseException as exc:
                detail = self._redact(exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes)
                                      else str(exc.smtp_error))[:200]
                if 400 <= exc.smtp_code < 500:
                    last_problem = f"temporary SMTP error {exc.smtp_code}: {detail}"
                else:
                    raise NotificationError(f"The mail server rejected the email ({exc.smtp_code}): {detail}",
                                            permanent=True, status_code=exc.smtp_code) from None
            except socket.timeout:
                # The server may already have accepted the message. Do not retry immediately;
                # the alert is marked failed and retried on a later run.
                raise NotificationError("SMTP request timed out.") from None
            except (smtplib.SMTPException, OSError) as exc:
                # Connection refused / dropped before the message was accepted: safe to retry.
                last_problem = f"connection error ({type(exc).__name__})"
            log.warning("Email %s, attempt %d/%d", last_problem, attempt, self.max_retries)
            if attempt < self.max_retries:
                self._sleep(self.backoff_seconds * (2 ** (attempt - 1)))
        raise NotificationError(f"Email unavailable after {self.max_retries} attempts: {last_problem}")

    def _send_once(self, msg: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self._implicit_tls:
            server = self._smtp_factory(self.host, self.port, timeout=self.timeout, context=context)
        else:
            server = self._smtp_factory(self.host, self.port, timeout=self.timeout)
        with server:
            if not self._implicit_tls:
                server.starttls(context=context)
            server.login(self.username, self._password)
            server.send_message(msg, from_addr=self.from_addr, to_addrs=self.to_addrs)
