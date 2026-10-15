"""Sends Binance alert messages through Twilio's WhatsApp API."""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

TWILIO_WHATSAPP_PREFIX = "whatsapp:"
PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")
MAX_RETRY_AFTER_SECONDS = 60


class NotificationError(Exception):
    """Raised when a WhatsApp notification could not be delivered."""

    def __init__(self, message: str, permanent: bool = False, status_code: Optional[int] = None):
        super().__init__(message)
        self.permanent = permanent
        self.status_code = status_code


class WhatsAppNotifier:
    """Twilio WhatsApp notifier for Binance Price Alert."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_number: str,
        session: Optional[requests.Session] = None,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.account_sid = (account_sid or "").strip()
        self.auth_token = (auth_token or "").strip()
        self.from_number = self._normalize_whatsapp_number(from_number, "TWILIO_WHATSAPP_FROM")
        self.to_number = self._normalize_whatsapp_number(to_number, "WHATSAPP_TO_NUMBER")
        if not self.account_sid:
            raise ValueError("TWILIO_ACCOUNT_SID is missing or malformed.")
        if not self.auth_token:
            raise ValueError("TWILIO_AUTH_TOKEN is missing or malformed.")
        if not PHONE_RE.match(self.from_number.replace("whatsapp:", "")):
            raise ValueError("TWILIO_WHATSAPP_FROM is missing or malformed.")
        if not PHONE_RE.match(self.to_number.replace("whatsapp:", "")):
            raise ValueError("WHATSAPP_TO_NUMBER is missing or malformed.")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep

    def __repr__(self) -> str:  # never leak credentials or phone numbers through logging/repr
        return "WhatsAppNotifier(account_sid=<redacted>, auth_token=<redacted>, to=<redacted>)"

    def _redact(self, text: str) -> str:
        for secret in (self.auth_token, self.account_sid):
            text = text.replace(secret, "<redacted>")
        return text

    @staticmethod
    def _normalize_whatsapp_number(value: str, env_name: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError(f"{env_name} is missing.")
        if text.lower().startswith("whatsapp:"):
            text = text.split(":", 1)[1]
        if not text.startswith("+"):
            text = f"+{text}"
        if not PHONE_RE.match(text):
            raise ValueError(f"{env_name} is missing or malformed.")
        return f"{TWILIO_WHATSAPP_PREFIX}{text}"

    @property
    def _url(self) -> str:
        return f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"

    def send_message(self, text: str) -> str:
        """Sends a WhatsApp message and returns the Twilio message SID."""
        payload = {"From": self.from_number, "To": self.to_number, "Body": text}
        last_problem = "no response"
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.post(self._url, data=payload, auth=(self.account_sid, self.auth_token), timeout=self.timeout)
            except requests.ConnectionError as exc:
                # The request most likely never reached Twilio, so retrying is safe.
                last_problem = f"connection error ({type(exc).__name__})"
                log.warning("WhatsApp %s, attempt %d/%d", last_problem, attempt, self.max_retries)
                self._backoff(attempt)
                continue
            except requests.RequestException as exc:
                # e.g. read timeout: Twilio may have accepted the message. Do not retry
                # immediately; the alert is marked failed and retried on a later run.
                raise NotificationError(f"WhatsApp request failed ({type(exc).__name__}).") from None

            try:
                data = resp.json()
            except ValueError:
                data = {}

            if 200 <= resp.status_code < 300:
                sid = data.get("sid") or data.get("message_sid") or str(resp.status_code)
                return str(sid)

            description = self._redact(str(data.get("message") or data.get("details") or f"HTTP {resp.status_code}"))[:200]
            if resp.status_code == 429:
                retry_after = self._retry_after(resp)
                last_problem = f"rate limited: {description}"
                if attempt < self.max_retries and retry_after <= MAX_RETRY_AFTER_SECONDS:
                    log.warning("WhatsApp rate limit; retrying in %ss", retry_after)
                    self._sleep(retry_after)
                    continue
                break
            if resp.status_code >= 500:
                last_problem = f"server error: {description}"
                log.warning("WhatsApp %s, attempt %d/%d", last_problem, attempt, self.max_retries)
                self._backoff(attempt)
                continue
            # 400 (bad number / sandbox not joined), 401 (bad credentials), 403 ...
            raise NotificationError(f"Twilio rejected the WhatsApp message: {description}", permanent=True, status_code=resp.status_code)

        raise NotificationError(f"WhatsApp unavailable after {self.max_retries} attempts: {last_problem}")

    def _backoff(self, attempt: int) -> None:
        if attempt < self.max_retries:
            self._sleep(self.backoff_seconds * (2 ** (attempt - 1)))

    def _retry_after(self, resp) -> float:
        try:
            return float(resp.headers.get("Retry-After", self.backoff_seconds))
        except (TypeError, ValueError):
            return self.backoff_seconds
