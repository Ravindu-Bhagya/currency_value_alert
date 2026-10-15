"""Alert evaluation and the price-check run.

Delivery semantics
------------------
1. When a price satisfies an alert, the alert is *claimed* atomically on the server
   (status active -> triggered, notification_status -> sending, with a 10-minute lease).
   Only the run that wins the claim sends the notification, so concurrent runs cannot
   both notify.
2. The run then calls WhatsApp:
     success -> notification_status = sent      (never sent again)
     failure -> notification_status = failed    (retried on following runs)
3. Failed notifications are re-claimed on later runs until MAX_NOTIFICATION_ATTEMPTS is
   reached. After that they stay "failed" (shown as "Notification Failed" in the UI) until
   the user clicks "Retry notification".
4. If a run dies after WhatsApp accepted the message but before "sent" was recorded, the
   lease expires and a later run sends it again. Delivery is therefore at-least-once:
   a rare duplicate is possible, a silently lost alert is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from .alert_repository import AlertRepository, Observation, RepositoryError
from .binance_client import BinanceClient, BinanceError
from .models import Alert, AlertStatus, Condition
from .whatsapp_notifier import NotificationError, WhatsAppNotifier

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_STORAGE_ERROR = 2
EXIT_BINANCE_ERROR = 3
EXIT_NOTIFICATION_ERROR = 4


def should_trigger(condition: Condition, current_price: Decimal, target_price: Decimal) -> bool:
    """Above: current >= target. Below: current <= target."""
    if condition is Condition.ABOVE:
        return current_price >= target_price
    if condition is Condition.BELOW:
        return current_price <= target_price
    raise ValueError(f"Unknown condition: {condition!r}")


def format_price(value: Decimal) -> str:
    """Formats a price without float rounding, keeping at least 2 and at most 8 decimals."""
    text = format(value.normalize(), "f")
    whole, _, frac = text.partition(".")
    frac = frac[:8].ljust(2, "0")
    return f"{whole}.{frac}"


def build_notification_message(alert: Alert, price: Decimal) -> str:
    quote = f" {alert.quote_asset}" if alert.quote_asset else ""
    return "\n".join(
        [
            "🔔 Binance Price Alert",
            "",
            f"Pair: {alert.display_pair}",
            f"Target Price: {format_price(alert.target_price)}{quote}",
            f"Condition: {alert.condition.value.capitalize()}",
            f"Current Price: {format_price(price)}{quote}",
            "Status: TARGET REACHED",
            "",
            "Your target price has been reached!",
        ]
    )


@dataclass
class RunSummary:
    active_checked: int = 0
    triggered: int = 0
    notifications_sent: int = 0
    notification_failures: int = 0
    skipped_concurrent: int = 0
    warnings: List[str] = field(default_factory=list)
    binance_unavailable: bool = False
    storage_errors: int = 0

    @property
    def exit_code(self) -> int:
        if self.storage_errors:
            return EXIT_STORAGE_ERROR
        if self.binance_unavailable:
            return EXIT_BINANCE_ERROR
        if self.notification_failures:
            return EXIT_NOTIFICATION_ERROR
        return EXIT_OK


class AlertService:
    def __init__(
        self,
        repository: AlertRepository,
        binance: BinanceClient,
        notifier: Optional[WhatsAppNotifier],
        max_notification_attempts: int = 5,
        dry_run: bool = False,
    ):
        if notifier is None and not dry_run:
            raise ValueError("A WhatsApp notifier is required unless dry_run is enabled.")
        self.repository = repository
        self.binance = binance
        self.notifier = notifier
        self.max_notification_attempts = max(1, max_notification_attempts)
        self.dry_run = dry_run

    def run(self) -> RunSummary:
        """Runs one check. RepositoryError from the initial load propagates to the caller."""
        summary = RunSummary()
        work = self.repository.fetch_work()
        if work.invalid_records:
            summary.warnings.append(f"{work.invalid_records} invalid alert record(s) skipped")

        active = [a for a in work.active if a.status is AlertStatus.ACTIVE]
        summary.active_checked = len(active)
        log.info("Loaded %d active alert(s), %d notification(s) to retry", len(active), len(work.pending_notifications))

        prices: Dict[str, Decimal] = {}
        if active:
            try:
                prices = self.binance.get_prices({a.symbol for a in active})
            except BinanceError as exc:
                summary.binance_unavailable = True
                log.error("Could not fetch Binance prices: %s", exc)

        if not summary.binance_unavailable:
            self._evaluate(active, prices, summary)

        self._retry_notifications(work.pending_notifications, summary)
        return summary

    def _evaluate(self, active: List[Alert], prices: Dict[str, Decimal], summary: RunSummary) -> None:
        observations: List[Observation] = []
        to_trigger = []
        for alert in active:
            price = prices.get(alert.symbol)
            if price is None:
                message = f"{alert.symbol} is not currently listed on Binance Spot"
                summary.warnings.append(message)
                log.warning("Alert %s: %s", alert.id, message)
                observations.append(Observation(alert.id, None, message))
                continue
            if should_trigger(alert.condition, price, alert.target_price):
                to_trigger.append((alert, price))
            else:
                observations.append(Observation(alert.id, price))

        if self.dry_run:
            for alert, price in to_trigger:
                log.info("[dry-run] Would trigger %s %s %s (price %s)", alert.display_pair,
                         alert.condition.value, alert.target_price, price)
            summary.triggered = len(to_trigger)
            return

        if observations:
            try:
                self.repository.record_observations(observations)
            except RepositoryError as exc:
                summary.storage_errors += 1
                log.error("Could not record observed prices: %s", exc)

        for alert, price in to_trigger:
            try:
                claimed = self.repository.claim_trigger(alert, price)
            except RepositoryError as exc:
                summary.storage_errors += 1
                log.error("Alert %s: could not mark as triggered: %s", alert.id, exc)
                continue
            if claimed is None:
                summary.skipped_concurrent += 1
                log.info("Alert %s was changed or claimed elsewhere; it will be re-evaluated next run", alert.id)
                continue
            summary.triggered += 1
            log.info("Alert %s triggered: %s %s %s at %s", alert.id, alert.display_pair,
                     alert.condition.value, alert.target_price, price)
            self._deliver(claimed, price, summary)

    def _retry_notifications(self, pending: List[Alert], summary: RunSummary) -> None:
        for alert in pending:
            if alert.notification_attempts >= self.max_notification_attempts:
                continue
            if self.dry_run:
                log.info("[dry-run] Would retry notification for alert %s", alert.id)
                continue
            try:
                claimed = self.repository.claim_notification(alert.id, self.max_notification_attempts)
            except RepositoryError as exc:
                summary.storage_errors += 1
                log.error("Alert %s: could not claim notification: %s", alert.id, exc)
                continue
            if claimed is None:
                summary.skipped_concurrent += 1
                continue
            price = claimed.triggered_price or claimed.last_price
            if price is None:
                log.error("Alert %s has no recorded trigger price; skipping", alert.id)
                continue
            log.info("Retrying notification for alert %s (attempt %d/%d)", alert.id,
                     claimed.notification_attempts, self.max_notification_attempts)
            self._deliver(claimed, price, summary)

    def _deliver(self, alert: Alert, price: Decimal, summary: RunSummary) -> None:
        assert self.notifier is not None
        try:
            self.notifier.send_message(build_notification_message(alert, price))
        except NotificationError as exc:
            summary.notification_failures += 1
            log.error("Alert %s: WhatsApp delivery failed: %s", alert.id, exc)
            self._record_result(alert.id, False, str(exc), summary)
            return
        summary.notifications_sent += 1
        log.info("Alert %s: WhatsApp notification delivered", alert.id)
        self._record_result(alert.id, True, None, summary)

    def _record_result(self, alert_id: str, delivered: bool, error: Optional[str], summary: RunSummary) -> None:
        try:
            self.repository.record_notification_result(alert_id, delivered, error)
        except RepositoryError as exc:
            summary.storage_errors += 1
            log.error("Alert %s: could not record notification result (%s); the lease will expire and "
                      "the notification will be re-attempted", alert_id, exc)
