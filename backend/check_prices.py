"""Command-line entry point for the scheduled price check.

Usage (from the repository root):
    python -m backend.check_prices                  # normal run
    python -m backend.check_prices --dry-run        # evaluate only: no writes, no WhatsApp
    python -m backend.check_prices --test-whatsapp  # send one test message

Environment variables:
    ALERTS_API_URL            URL of the alert storage Worker (https://...workers.dev)
    CHECKER_TOKEN             Worker token for the checker (/internal/* endpoints)
    TWILIO_ACCOUNT_SID        Twilio account SID for WhatsApp delivery
    TWILIO_AUTH_TOKEN         Twilio auth token for WhatsApp delivery
    TWILIO_WHATSAPP_FROM      Twilio WhatsApp sender number, e.g. whatsapp:+14155238886
    WHATSAPP_TO_NUMBER        Destination WhatsApp number, e.g. +94705049478
    MAX_NOTIFICATION_ATTEMPTS Optional, default 5
    BINANCE_BASE_URLS         Optional, comma-separated list of Binance API hosts

Exit codes: 0 ok, 1 configuration error, 2 alert storage error,
            3 Binance unavailable, 4 WhatsApp delivery failed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional

from .alert_repository import AlertRepository, RepositoryError
from .alert_service import (
    EXIT_CONFIG_ERROR,
    EXIT_NOTIFICATION_ERROR,
    EXIT_OK,
    EXIT_STORAGE_ERROR,
    AlertService,
)
from .binance_client import BinanceClient
from .whatsapp_notifier import NotificationError, WhatsAppNotifier

log = logging.getLogger("price_checker")


class SecretRedactingFilter(logging.Filter):
    """Belt-and-braces: scrub secret values from any log line."""

    def __init__(self, secrets: List[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "<redacted>")
        record.msg, record.args = message, None
        return True


def configure_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    handler.addFilter(SecretRedactingFilter(
        [os.environ.get(k, "") for k in ("TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID", "CHECKER_TOKEN", "WHATSAPP_TO_NUMBER", "TWILIO_WHATSAPP_FROM")]
    ))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # urllib3 debug logs include request URLs, which contain the Twilio account SID.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise ValueError(f"Environment variable {name} is not set.")
    return value


def build_notifier() -> WhatsAppNotifier:
    return WhatsAppNotifier(
        env("TWILIO_ACCOUNT_SID"),
        env("TWILIO_AUTH_TOKEN"),
        env("TWILIO_WHATSAPP_FROM"),
        env("WHATSAPP_TO_NUMBER"),
    )


def build_binance() -> BinanceClient:
    urls = [u.strip() for u in env("BINANCE_BASE_URLS", required=False).split(",") if u.strip()]
    return BinanceClient(base_urls=urls or None)


def cmd_test_whatsapp() -> int:
    notifier = build_notifier()
    notifier.send_message(
        "✅ Binance Price Alert\n\nTest message: your WhatsApp notifications are configured correctly."
    )
    log.info("Test message delivered.")
    return EXIT_OK


def cmd_test_telegram() -> int:
    log.warning("The --test-telegram flag is deprecated; using WhatsApp instead.")
    return cmd_test_whatsapp()


def cmd_check(dry_run: bool) -> int:
    repository = AlertRepository(env("ALERTS_API_URL"), env("CHECKER_TOKEN"))
    notifier = None if dry_run else build_notifier()
    max_attempts = int(env("MAX_NOTIFICATION_ATTEMPTS", required=False) or 5)
    service = AlertService(repository, build_binance(), notifier, max_attempts, dry_run=dry_run)
    try:
        summary = service.run()
    except RepositoryError as exc:
        log.error("Could not load alerts: %s", exc)
        return EXIT_STORAGE_ERROR
    for warning in summary.warnings:
        log.warning(warning)
    log.info(
        "Done%s: %d active checked, %d triggered, %d notification(s) sent, %d failed, %d skipped (concurrent)",
        " [dry-run]" if dry_run else "",
        summary.active_checked, summary.triggered, summary.notifications_sent,
        summary.notification_failures, summary.skipped_concurrent,
    )
    return summary.exit_code


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Check Binance prices and send WhatsApp alerts.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="evaluate alerts without writing or notifying")
    mode.add_argument("--test-whatsapp", action="store_true", help="send a single WhatsApp test message")
    mode.add_argument("--test-telegram", action="store_true", help="deprecated alias for --test-whatsapp")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    try:
        if args.test_whatsapp or args.test_telegram:
            return cmd_test_whatsapp()
        return cmd_check(args.dry_run)
    except ValueError as exc:
        log.error("Configuration error: %s", exc)
        return EXIT_CONFIG_ERROR
    except NotificationError as exc:
        log.error("WhatsApp error: %s", exc)
        return EXIT_NOTIFICATION_ERROR

if __name__ == "__main__":
    sys.exit(main())
