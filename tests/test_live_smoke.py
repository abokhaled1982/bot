"""
tests/test_live_smoke.py — OPT-IN Smoke-Test mit ECHTEM Geld.

Laeuft NUR, wenn beides explizit gesetzt ist:

    LIVE_SMOKE=1  DRY_RUN=False  python3 -m pytest tests/test_live_smoke.py -s

Ohne diese Variablen wird der Test uebersprungen — `pytest` ohne Argumente
loest also niemals eine echte Order aus.

Was er macht: kauft den kleinstmoeglichen Betrag (Default 1 USDC, automatisch
auf Binances `minNotional` angehoben) und verkauft ihn sofort wieder. Damit ist
belegt, dass API-Key, Permissions, Symbol-Filter und der Kauf-/Verkauf-Pfad in
`real_executor` produktiv funktionieren. Es wird bewusst KEIN OCO gesetzt,
damit keine offene Order zurueckbleibt.

Kosten: nur Spread + 2x Taker-Gebuehr auf den Mini-Betrag.
"""
from __future__ import annotations

import os

import pytest

from src.commands.handler import QUOTE_ASSET
from src.execution import real_executor as ex

SYMBOL = os.getenv("LIVE_SMOKE_SYMBOL", f"BTC{QUOTE_ASSET}").upper()
WANT_USDT = float(os.getenv("LIVE_SMOKE_USDT", "1.0"))
MAX_USDT = float(os.getenv("LIVE_SMOKE_MAX_USDT", "12.0"))

pytestmark = pytest.mark.skipif(
    os.getenv("LIVE_SMOKE") != "1" or ex.DRY_RUN,
    reason="Live-Smoke-Test: nur mit LIVE_SMOKE=1 und DRY_RUN=False",
)


def test_live_buy_and_sell_roundtrip():
    """Echter Mini-Kauf + sofortiger Verkauf auf Binance Spot."""
    info = ex.get_symbol_info(SYMBOL)
    assert info, f"Symbol-Info fuer {SYMBOL} nicht abrufbar"

    amount = max(WANT_USDT, info["minNotional"] * 1.05)
    if amount > MAX_USDT:
        pytest.skip(
            f"{SYMBOL} verlangt minNotional ${info['minNotional']:.2f} → "
            f"${amount:.2f} > Limit ${MAX_USDT:.2f}. Anderes Symbol waehlen "
            f"(LIVE_SMOKE_SYMBOL) oder LIVE_SMOKE_MAX_USDT erhoehen."
        )

    balance = ex.get_account_balance(QUOTE_ASSET)
    if balance < amount:
        pytest.skip(f"Guthaben ${balance:.2f} {QUOTE_ASSET} < ${amount:.2f}")

    buy = ex.market_buy(SYMBOL, amount, trader="SMOKE", coin=SYMBOL)
    print(f"\nBUY : {buy.summary()}")
    assert buy.ok, f"BUY fehlgeschlagen: {buy.reason} (code {buy.error_code})"
    assert buy.qty > 0

    sell = ex.market_sell(SYMBOL, buy.qty, trader="SMOKE", coin=SYMBOL)
    print(f"SELL: {sell.summary()}")
    assert sell.ok, f"SELL fehlgeschlagen: {sell.reason} (code {sell.error_code})"

    cost = buy.total_usdt - sell.total_usdt
    print(f"Roundtrip-Kosten: {cost:+.4f} {QUOTE_ASSET}")
