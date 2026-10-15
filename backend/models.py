"""Domain models and input validation for price alerts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping, Optional

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,20}$")
ALERT_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ValidationError(ValueError):
    """Raised when an alert or user-supplied value is invalid."""


class Condition(str, Enum):
    ABOVE = "above"  # trigger when current_price >= target_price
    BELOW = "below"  # trigger when current_price <= target_price


class AlertStatus(str, Enum):
    ACTIVE = "active"
    TRIGGERED = "triggered"
    CANCELLED = "cancelled"


class NotificationStatus(str, Enum):
    NONE = "none"        # alert has not triggered yet
    PENDING = "pending"  # triggered, waiting to be (re)sent
    SENDING = "sending"  # a checker run holds a lease and is sending right now
    SENT = "sent"        # WhatsApp confirmed delivery
    FAILED = "failed"    # last attempt failed; retried on later runs up to the attempt limit


def parse_price(value: Any) -> Decimal:
    """Parses a strictly positive, finite decimal price."""
    if isinstance(value, bool) or value is None:
        raise ValidationError("Price is required.")
    try:
        price = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValidationError(f"Invalid price: {value!r}") from None
    if not price.is_finite() or price <= 0:
        raise ValidationError(f"Price must be greater than zero: {value!r}")
    return price


def normalize_symbol(value: Any) -> str:
    """Converts 'xrp/usdt' or 'XRPUSDT' into the Binance symbol 'XRPUSDT'."""
    if not isinstance(value, str):
        raise ValidationError("Symbol is required.")
    symbol = value.strip().upper().replace("/", "")
    if not SYMBOL_PATTERN.match(symbol):
        raise ValidationError(f"Invalid trading symbol: {value!r}")
    return symbol


def _optional_price(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    return parse_price(value)


@dataclass(frozen=True)
class Alert:
    id: str
    symbol: str
    target_price: Decimal
    condition: Condition
    status: AlertStatus
    notification_status: NotificationStatus = NotificationStatus.NONE
    notification_attempts: int = 0
    version: int = 1
    base_asset: str = ""
    quote_asset: str = ""
    last_price: Optional[Decimal] = None
    triggered_price: Optional[Decimal] = None
    triggered_at: Optional[str] = None
    created_at: Optional[str] = None

    @property
    def display_pair(self) -> str:
        if self.base_asset and self.quote_asset:
            return f"{self.base_asset}/{self.quote_asset}"
        return self.symbol

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Alert":
        """Builds a validated Alert from an API record. Raises ValidationError."""
        alert_id = str(data.get("id", ""))
        if not ALERT_ID_PATTERN.match(alert_id):
            raise ValidationError(f"Invalid alert id: {alert_id!r}")
        try:
            condition = Condition(data.get("condition"))
            status = AlertStatus(data.get("status"))
            notification_status = NotificationStatus(data.get("notification_status") or "none")
            attempts = int(data.get("notification_attempts") or 0)
            version = int(data.get("version") or 1)
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"Alert {alert_id} has invalid fields: {exc}") from None
        return cls(
            id=alert_id,
            symbol=normalize_symbol(data.get("symbol")),
            target_price=parse_price(data.get("target_price")),
            condition=condition,
            status=status,
            notification_status=notification_status,
            notification_attempts=attempts,
            version=version,
            base_asset=str(data.get("base_asset") or ""),
            quote_asset=str(data.get("quote_asset") or ""),
            last_price=_optional_price(data.get("last_price")),
            triggered_price=_optional_price(data.get("triggered_price")),
            triggered_at=data.get("triggered_at"),
            created_at=data.get("created_at"),
        )
