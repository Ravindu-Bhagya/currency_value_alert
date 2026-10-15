"""Command-line entry point for the scheduled price check.

Usage (from the repository root):
    python -m backend.check_prices                  # normal run
    python -m backend.check_prices --dry-run        # evaluate only: no writes, no email
    python -m backend.check_prices --test-email     # send one test email

Environment variables:
    ALERTS_API_URL            URL of the alert storage Worker (https://...workers.dev)
    CHECKER_TOKEN             Worker token for the checker (/internal/* endpoints)
    SMTP_HOST                 Mail server, default smtp.gmail.com
    SMTP_PORT                 Default 587 (STARTTLS); 465 uses implicit TLS
    SMTP_USERNAME             Mail account login, e.g. you@gmail.com
    SMTP_PASSWORD             Mail account password (Gmail: an App Password)
    EMAIL_FROM                Sender address, default SMTP_USERNAME
    EMAIL_TO                  Recipient address(es), comma-separated
    MAX_NOTIFICATION_ATTEMPTS Optional, default 5
    BINANCE_BASE_URLS         Optional, comma-separated list of Binance API hosts

Exit codes: 0 ok, 1 configuration error, 2 alert storage error,
            3 Binance unavailable, 4 email delivery failed.
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
from .email_notifier import EmailNotifier, NotificationError, parse_recipients

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
        [os.environ.get(k, "") for k in ("SMTP_PASSWORD", "CHECKER_TOKEN")]
    ))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # urllib3 debug logs include request URLs; keep them out of the Actions log.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise ValueError(f"Environment variable {name} is not set.")
    return value


def build_notifier() -> EmailNotifier:
    port = env("SMTP_PORT", required=False) or "587"
    if not port.isdigit():
        raise ValueError("SMTP_PORT must be a number.")
    return EmailNotifier(
        env("SMTP_HOST", required=False) or "smtp.gmail.com",
        int(port),
        env("SMTP_USERNAME"),
        env("SMTP_PASSWORD"),
        env("EMAIL_FROM", required=False),
        parse_recipients(env("EMAIL_TO")),
    )


def build_binance() -> BinanceClient:
    urls = [u.strip() for u in env("BINANCE_BASE_URLS", required=False).split(",") if u.strip()]
    return BinanceClient(base_urls=urls or None)


def cmd_test_email() -> int:
    notifier = build_notifier()
    notifier.send_message(
        "✅ Binance Price Alert\n\nTest message: your email notifications are configured correctly.",
        subject="✅ Binance Price Alert: test email",
    )
    log.info("Test email delivered to %s.", ", ".join(notifier.to_addrs))
    return EXIT_OK


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
    parser = argparse.ArgumentParser(description="Check Binance prices and send email alerts.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="evaluate alerts without writing or notifying")
    mode.add_argument("--test-email", action="store_true", help="send a single test email")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    try:
        if args.test_email:
            return cmd_test_email()
        return cmd_check(args.dry_run)
    except ValueError as exc:
        log.error("Configuration error: %s", exc)
        return EXIT_CONFIG_ERROR
    except NotificationError as exc:
        log.error("Email error: %s", exc)
        return EXIT_NOTIFICATION_ERROR

if __name__ == "__main__":
    sys.exit(main())
