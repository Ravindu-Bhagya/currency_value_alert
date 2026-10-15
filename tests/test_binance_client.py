from decimal import Decimal

import pytest
import requests

from backend.binance_client import BinanceClient, BinanceError, InvalidSymbolError
from backend.models import ValidationError
from tests.conftest import FakeResponse, FakeSession

TICKERS = [{"symbol": "XRPUSDT", "price": "2.50300000"}, {"symbol": "BTCUSDT", "price": "67000.01000000"}]


def client(responses, **kwargs):
    sleeps = []
    session = FakeSession(responses)
    c = BinanceClient(session=session, sleep=sleeps.append, **kwargs)
    return c, session, sleeps


def test_get_prices_uses_one_request_and_decimals():
    c, session, _ = client([FakeResponse(200, TICKERS)])
    prices = c.get_prices(["XRPUSDT", "btc/usdt", "NOPEUSDT"])
    assert prices == {"XRPUSDT": Decimal("2.503"), "BTCUSDT": Decimal("67000.01")}
    assert len(session.calls) == 1
    assert session.calls[0]["timeout"] == 10.0
    assert session.calls[0]["url"] == "https://data-api.binance.vision/api/v3/ticker/price"


def test_retries_server_errors_then_succeeds():
    c, session, sleeps = client([FakeResponse(503), requests.ConnectionError("boom"), FakeResponse(200, TICKERS)])
    assert c.get_all_prices()["XRPUSDT"] == Decimal("2.503")
    assert len(session.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_rate_limit_respects_retry_after():
    c, _, sleeps = client([FakeResponse(429, {}, headers={"Retry-After": "7"}), FakeResponse(200, TICKERS)])
    c.get_all_prices()
    assert sleeps == [7.0]


def test_restricted_location_falls_back_to_next_host():
    c, session, _ = client([FakeResponse(451, text="restricted"), FakeResponse(200, TICKERS)])
    c.get_all_prices()
    assert [call["url"].rsplit("/api/v3", 1)[0] for call in session.calls] == [
        "https://data-api.binance.vision",
        "https://api.binance.com",
    ]


def test_all_hosts_failing_raises():
    c, _, _ = client([FakeResponse(500)] * 6)
    with pytest.raises(BinanceError):
        c.get_all_prices()


def test_ip_ban_is_not_retried():
    c, session, _ = client([FakeResponse(418)])
    with pytest.raises(BinanceError):
        c.get_all_prices()
    assert len(session.calls) == 1


def test_invalid_symbol_error():
    c, _, _ = client([FakeResponse(400, {"code": -1121, "msg": "Invalid symbol."})])
    with pytest.raises(InvalidSymbolError):
        c.get_price("FAKEUSDT")


def test_symbol_validated_before_request():
    c, session, _ = client([])
    with pytest.raises(ValidationError):
        c.get_price("BAD SYMBOL")
    assert session.calls == []


def test_invalid_json_raises():
    c, _, _ = client([FakeResponse(200, None)])
    with pytest.raises(BinanceError):
        c.get_all_prices()


def test_malformed_prices_are_skipped():
    c, _, _ = client([FakeResponse(200, [{"symbol": "AUSDT", "price": "x"}, {"symbol": "BUSDT", "price": "0"}, TICKERS[0]])])
    assert c.get_all_prices() == {"XRPUSDT": Decimal("2.503")}
