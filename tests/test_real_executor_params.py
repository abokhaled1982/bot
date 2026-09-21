"""
tests/test_real_executor_params.py — Schutz vor Binance-Fehler -1100
("Illegal characters found in parameter 'quantity'").

Kleine Mengen (z.B. 6 USDC in BTC -> 0.0000693) werden von Python als
`6.93e-05` formatiert; Binance akzeptiert nur Plain-Decimal. Diese Tests
pruefen die Formatierung und dass die gesendeten Order-Parameter sauber sind.
"""
from __future__ import annotations

import pytest

from src.execution import real_executor as ex

SYMBOL = "BTCUSDC"
INFO = {
    "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDC",
    "minQty": 1e-05, "stepSize": 1e-08, "tickSize": 0.01,
    "minNotional": 5.0, "isSpotAllowed": True,
}


@pytest.mark.parametrize("value, expected", [
    (6.93e-05, "0.0000693"),
    (0.00012, "0.00012"),
    (1e-08, "0.00000001"),
    (100000.0, "100000"),
    (0.0, "0"),
])
def test_fmt_never_uses_scientific_notation(value, expected):
    assert ex._fmt(value) == expected
    assert "e" not in ex._fmt(value).lower()


@pytest.fixture
def capture_orders(monkeypatch):
    """Order-Requests abfangen statt zu senden."""
    sent: list[dict] = []

    def fake_request(method, path, params=None, signed=False, retries=0):
        sent.append({"path": path, "params": dict(params or {})})
        return {"orderId": 1, "status": "FILLED", "executedQty": "0.0000693",
                "cummulativeQuoteQty": "6.00", "orderListId": 7}

    monkeypatch.setattr(ex, "DRY_RUN", False)
    monkeypatch.setattr(ex, "_request", fake_request)
    monkeypatch.setattr(ex, "get_symbol_info", lambda symbol: dict(INFO))
    monkeypatch.setattr(ex, "get_price", lambda symbol: 86_586.91)
    return sent


def test_market_buy_sends_plain_decimal_quantity(capture_orders):
    """6 USDC BTC -> quantity darf nicht als 6.93e-05 rausgehen."""
    result = ex.market_buy(SYMBOL, 6.0, trader="SMOKE", coin="BTC")

    assert result.ok
    qty = capture_orders[0]["params"]["quantity"]
    assert qty == "0.00006929"
    assert "e" not in qty.lower()


def test_market_sell_sends_plain_decimal_quantity(capture_orders):
    ex.market_sell(SYMBOL, 6.93e-05, trader="SMOKE", coin="BTC")

    assert capture_orders[0]["params"]["quantity"] == "0.0000693"


def test_oco_sends_plain_decimal_prices(capture_orders):
    ex.place_oco_exit(SYMBOL, 6.93e-05, 86_586.91)

    params = capture_orders[0]["params"]
    for key in ("quantity", "price", "stopPrice", "stopLimitPrice"):
        assert "e" not in str(params[key]).lower(), key
