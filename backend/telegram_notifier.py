"""Sends messages through the official Telegram Bot API (https://core.telegram.org/bots/api)."""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TOKEN_PATTERN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
CHAT_ID_PATTERN = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")
MAX_RETRY_AFTER_SECONDS = 60


class TelegramError(Exception):
    """A message could not be delivered.

    permanent=True means retrying will not help without a configuration change
    (e.g. wrong token or chat ID). It is still retried on later runs up to the
    attempt limit, in case the user fixes the configuration in the meantime.
    """

    def __init__(self, message: str, permanent: bool = False, status_code: Optional[int] = None):
        super().__init__(message)
        self.permanent = permanent
        self.status_code = status_code


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        session: Optional[requests.Session] = None,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        token = (token or "").strip()
        chat_id = (chat_id or "").strip()
        # Error messages deliberately never include the values themselves.
        if not TOKEN_PATTERN.match(token):
            raise ValueError("TELEGRAM_BOT_TOKEN is missing or malformed.")
        if not CHAT_ID_PATTERN.match(chat_id):
            raise ValueError("TELEGRAM_CHAT_ID is missing or malformed.")
        self._token = token
        self.chat_id = chat_id
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep

    def __repr__(self) -> str:  # never leak the token through logging/repr
        return "TelegramNotifier(chat_id=<redacted>, token=<redacted>)"

    def _redact(self, text: str) -> str:
        return str(text).replace(self._token, "<redacted>")

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self._token}/{method}"

    def send_message(self, text: str) -> int:
        """Sends a plain-text message. Returns the Telegram message_id or raises TelegramError."""
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        last_problem = "no response"
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.post(self._url("sendMessage"), json=payload, timeout=self.timeout)
            except requests.ConnectionError as exc:
                # The request most likely never reached Telegram, so retrying is safe.
                last_problem = f"connection error ({type(exc).__name__})"
                log.warning("Telegram %s, attempt %d/%d", last_problem, attempt, self.max_retries)
                self._backoff(attempt)
                continue
            except requests.RequestException as exc:
                # e.g. read timeout: Telegram may have delivered the message. Do not retry
                # immediately; the alert is marked failed and retried on a later run.
                raise TelegramError(f"Telegram request failed ({type(exc).__name__}).") from None

            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code == 200 and data.get("ok"):
                return int(data.get("result", {}).get("message_id", 0))

            description = self._redact(str(data.get("description") or f"HTTP {resp.status_code}"))[:200]
            if resp.status_code == 429:
                retry_after = (data.get("parameters") or {}).get("retry_after", self.backoff_seconds)
                last_problem = f"rate limited: {description}"
                if attempt < self.max_retries and float(retry_after) <= MAX_RETRY_AFTER_SECONDS:
                    log.warning("Telegram rate limit; retrying in %ss", retry_after)
                    self._sleep(float(retry_after))
                    continue
                break
            if resp.status_code >= 500:
                last_problem = f"server error: {description}"
                log.warning("Telegram %s, attempt %d/%d", last_problem, attempt, self.max_retries)
                self._backoff(attempt)
                continue
            # 400 (bad chat id), 401 (bad token), 403 (bot blocked / never started) ...
            raise TelegramError(f"Telegram rejected the message: {description}", permanent=True, status_code=resp.status_code)
        raise TelegramError(f"Telegram unavailable after {self.max_retries} attempts: {last_problem}")

    def _backoff(self, attempt: int) -> None:
        if attempt < self.max_retries:
            self._sleep(self.backoff_seconds * (2 ** (attempt - 1)))

    def find_chat_ids(self) -> List[Tuple[str, str]]:
        """Returns (chat_id, display name) pairs from recent messages sent to the bot."""
        try:
            resp = self.session.get(self._url("getUpdates"), timeout=self.timeout)
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise TelegramError(f"getUpdates failed ({type(exc).__name__}).") from None
        if not data.get("ok"):
            raise TelegramError(self._redact(f"getUpdates failed: {data.get('description', resp.status_code)}"))
        found = {}
        for update in data.get("result", []):
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            if "id" in chat:
                name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                found[str(chat["id"])] = name or chat.get("username") or ""
        return list(found.items())
