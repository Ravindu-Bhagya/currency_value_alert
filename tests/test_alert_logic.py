from decimal import Decimal

import pytest

from backend.alert_service import (
    EXIT_BINANCE_ERROR,
    EXIT_NOTIFICATION_ERROR,
    EXIT_OK,
    AlertService,
    build_notification_message,
    format_price,
    should_trigger,
)
from backend.binance_client import BinanceError
from backend.models import Alert, Condition, ValidationError, normalize_symbol, parse_price
from backend.email_notifier import NotificationError
from tests.conftest import FakeBinance, FakeNotifier, InMemoryRepository, make_record


def service(repo, binance, notifier, **kwargs):
    return AlertService(repo, binance, notifier, max_notification_attempts=kwargs.pop("max_attempts", 3), **kwargs)


# ------------------------------------------------------------------ pure evaluation

@pytest.mark.parametrize(
    "condition,current,target,expected",
    [
        (Condition.ABOVE, "2.51", "2.50", True),
        (Condition.ABOVE, "2.49", "2.50", False),
        (Condition.ABOVE, "2.50", "2.50", True),        # exactly equal triggers
        (Condition.BELOW, "2.49", "2.50", True),
        (Condition.BELOW, "2.51", "2.50", False),
        (Condition.BELOW, "2.50000000", "2.5", True),   # equal despite different precision
        (Condition.ABOVE, "0.00000002", "0.00000001", True),
    ],
)
def test_should_trigger(condition, current, target, expected):
    assert should_trigger(condition, Decimal(current), Decimal(target)) is expected


def test_decimal_comparison_avoids_float_errors():
    # 0.1 + 0.2 != 0.3 in floating point; Decimal handles it exactly.
    assert should_trigger(Condition.BELOW, Decimal("0.1") + Decimal("0.2"), Decimal("0.3"))


@pytest.mark.parametrize("value", ["0", "-1", "abc", "", None, "NaN", "Infinity", True, "1e"])
def test_invalid_target_prices_rejected(value):
    with pytest.raises(ValidationError):
        parse_price(value)


@pytest.mark.parametrize("value", ["", "BTC-USDT", "<script>", "X", 123, None, "A" * 25])
def test_invalid_symbols_rejected(value):
    with pytest.raises(ValidationError):
        normalize_symbol(value)


def test_symbol_normalisation():
    assert normalize_symbol(" xrp/usdt ") == "XRPUSDT"


def test_invalid_alert_record_rejected():
    with pytest.raises(ValidationError):
        Alert.from_dict(make_record(condition="sideways"))
    with pytest.raises(ValidationError):
        Alert.from_dict(make_record(id="../../etc"))


def test_message_format():
    alert = Alert.from_dict(make_record(target_price="2.5"))
    message = build_notification_message(alert, Decimal("2.50300000"))
    assert "🔔 Binance Price Alert" in message
    assert "Pair: XRP/USDT" in message
    assert "Target Price: 2.50 USDT" in message
    assert "Condition: Above" in message
    assert "Current Price: 2.503 USDT" in message
    assert "Status: TARGET REACHED" in message


def test_format_price():
    assert format_price(Decimal("67000")) == "67000.00"
    assert format_price(Decimal("0.00001234")) == "0.00001234"
    assert format_price(Decimal("1E+2")) == "100.00"


# ------------------------------------------------------------------ full runs

def test_above_alert_triggers_and_notifies_once():
    repo = InMemoryRepository()
    rec = repo.add(target_price="2.5", condition="above")
    notifier = FakeNotifier()
    summary = service(repo, FakeBinance({"XRPUSDT": "2.6"}), notifier).run()
    assert summary.triggered == 1 and summary.notifications_sent == 1
    assert summary.exit_code == EXIT_OK
    assert repo.records[rec["id"]]["status"] == "triggered"
    assert repo.records[rec["id"]]["notification_status"] == "sent"
    assert len(notifier.sent) == 1

    # A subsequent run must not notify again.
    summary2 = service(repo, FakeBinance({"XRPUSDT": "2.7"}), notifier).run()
    assert summary2.triggered == 0
    assert len(notifier.sent) == 1


def test_below_alert_triggers():
    repo = InMemoryRepository()
    repo.add(target_price="2.5", condition="below")
    notifier = FakeNotifier()
    summary = service(repo, FakeBinance({"XRPUSDT": "2.4"}), notifier).run()
    assert summary.triggered == 1
    assert "Condition: Below" in notifier.sent[0]


def test_condition_not_met_records_observation_only():
    repo = InMemoryRepository()
    rec = repo.add(target_price="2.5", condition="above")
    notifier = FakeNotifier()
    summary = service(repo, FakeBinance({"XRPUSDT": "2.4"}), notifier).run()
    assert summary.triggered == 0
    assert notifier.sent == []
    assert repo.records[rec["id"]]["status"] == "active"
    assert repo.records[rec["id"]]["last_price"] == "2.4"


def test_price_exactly_equal_triggers():
    repo = InMemoryRepository()
    repo.add(target_price="2.5", condition="below")
    summary = service(repo, FakeBinance({"XRPUSDT": "2.50000000"}), FakeNotifier()).run()
    assert summary.triggered == 1


def test_cancelled_alerts_are_ignored():
    repo = InMemoryRepository()
    rec = repo.add(status="cancelled", target_price="1", condition="above")
    binance = FakeBinance({"XRPUSDT": "5"})
    notifier = FakeNotifier()
    summary = service(repo, binance, notifier).run()
    assert summary.active_checked == 0 and notifier.sent == []
    assert binance.calls == 0  # no unnecessary API call
    assert repo.records[rec["id"]]["status"] == "cancelled"


def test_already_triggered_alert_is_not_renotified():
    repo = InMemoryRepository()
    repo.add(status="triggered", notification_status="sent", notification_attempts=1, triggered_price="3")
    notifier = FakeNotifier()
    service(repo, FakeBinance({"XRPUSDT": "5"}), notifier).run()
    assert notifier.sent == []


def test_unknown_symbol_does_not_stop_other_alerts():
    repo = InMemoryRepository()
    delisted = repo.add(symbol="GONEUSDT", base_asset="GONE")
    ok = repo.add(target_price="1")
    notifier = FakeNotifier()
    summary = service(repo, FakeBinance({"XRPUSDT": "2"}), notifier).run()
    assert summary.triggered == 1
    assert repo.records[ok["id"]]["status"] == "triggered"
    assert repo.records[delisted["id"]]["status"] == "active"
    assert any(o.alert_id == delisted["id"] and o.error for o in repo.observations)
    assert summary.warnings


def test_binance_outage_keeps_alerts_active_and_exits_nonzero():
    repo = InMemoryRepository()
    rec = repo.add(target_price="1")
    summary = service(repo, FakeBinance(error=BinanceError("down")), FakeNotifier()).run()
    assert summary.exit_code == EXIT_BINANCE_ERROR
    assert repo.records[rec["id"]]["status"] == "active"


def test_email_failure_marks_failed_then_retry_succeeds():
    repo = InMemoryRepository()
    rec = repo.add(target_price="1")
    notifier = FakeNotifier(fail_times=1, error=NotificationError("HTTP 502"))
    summary = service(repo, FakeBinance({"XRPUSDT": "2"}), notifier).run()
    assert summary.triggered == 1 and summary.notification_failures == 1
    assert summary.exit_code == EXIT_NOTIFICATION_ERROR
    record = repo.records[rec["id"]]
    assert record["status"] == "triggered"           # the trigger itself is never lost
    assert record["notification_status"] == "failed"
    assert record["notification_error"] == "HTTP 502"

    # Next run: price has moved away, but the notification is still retried with the trigger price.
    summary2 = service(repo, FakeBinance({"XRPUSDT": "0.5"}), notifier).run()
    assert summary2.notifications_sent == 1
    assert record["notification_status"] == "sent"
    assert "Current Price: 2.00 USDT" in notifier.sent[0]

    # And never again after success.
    service(repo, FakeBinance({"XRPUSDT": "2"}), notifier).run()
    assert len(notifier.sent) == 1


def test_notification_retries_stop_at_max_attempts():
    repo = InMemoryRepository()
    rec = repo.add(target_price="1")
    notifier = FakeNotifier(fail_times=99, error=NotificationError("Unauthorized", permanent=True))
    for _ in range(5):
        service(repo, FakeBinance({"XRPUSDT": "2"}), notifier, max_attempts=3).run()
    assert repo.records[rec["id"]]["notification_attempts"] == 3
    assert repo.records[rec["id"]]["notification_status"] == "failed"
    assert notifier.fail_times == 99 - 3


def test_concurrent_runs_notify_only_once():
    """Two runs that read the same alert before either claims it: only one may notify."""
    repo = InMemoryRepository()
    repo.add(target_price="1")
    stale_work = repo.fetch_work()  # run B loaded its work at the same time as run A
    notifier = FakeNotifier()
    service(repo, FakeBinance({"XRPUSDT": "2"}), notifier).run()  # run A

    original = repo.fetch_work
    repo.fetch_work = lambda: stale_work  # run B acts on the stale snapshot
    summary_b = service(repo, FakeBinance({"XRPUSDT": "2"}), notifier).run()
    repo.fetch_work = original
    assert summary_b.skipped_concurrent == 1
    assert len(notifier.sent) == 1


def test_dry_run_does_not_write_or_notify():
    repo = InMemoryRepository()
    rec = repo.add(target_price="1")
    summary = AlertService(repo, FakeBinance({"XRPUSDT": "2"}), None, dry_run=True).run()
    assert summary.triggered == 1
    assert repo.records[rec["id"]]["status"] == "active"
    assert repo.observations == []


def test_storage_failure_when_recording_result_is_reported():
    repo = InMemoryRepository()
    repo.add(target_price="1")
    repo.fail_results = True
    summary = service(repo, FakeBinance({"XRPUSDT": "2"}), FakeNotifier()).run()
    assert summary.storage_errors == 1
    assert summary.exit_code != EXIT_OK
