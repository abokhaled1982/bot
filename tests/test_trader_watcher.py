"""
tests/test_trader_watcher.py — Beweis, dass die Ueberwachung eines abonnierten
Traders wirklich funktioniert, OHNE `live_copytrader.py` als Prozess zu starten.

Deckt genau die Kette ab, die live im Betrieb laeuft:
  data/copy_traders.json --wird eingelesen--> CopyTraderMonitor
      --pollt Positionen--> neue Long-Position --> COPY_OPEN_LONG-Signal

Netzwerk (Binance) wird ueber `fetch_other_positions` gemockt (monkeypatch),
`copy_traders.json` und die SQLite-DB werden auf tmp_path umgeleitet, damit
kein echter Bot-State beruehrt wird. Da `pytest-asyncio` nicht installiert
ist, treiben die Tests die Coroutinen selbst per `asyncio.run(...)` an.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src.adapters import binance_leaderboard as bl
from src.monitoring import CopyTraderMonitor
from src.utils import trader_store

WALLET = "5041579634804156928"


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """copy_traders.json + SQLite-DB des Traders-Stores auf tmp_path umleiten."""
    traders_file = tmp_path / "copy_traders.json"
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(trader_store, "TRADERS_FILE", str(traders_file))
    monkeypatch.setattr(trader_store, "DB_PATH", str(db_file))
    return traders_file


def _write_subscribed_trader(traders_file, *, size_usdt: float = 30.0) -> None:
    traders_file.write_text(json.dumps([{
        "wallet": WALLET, "size_usdt": size_usdt, "is_copied": 1, "is_focus": 0,
        "note": "", "source": "dashboard", "account_usd": 0.0,
        "win_rate": 0.0, "trades": 0, "added_at": 0.0, "updated_at": 0.0,
    }]))


def _position_row(*, coin_symbol: str = "BTCUSDT", amount: str = "0.001",
                   entry_price: str = "50000") -> dict:
    return {
        "symbol": coin_symbol, "amount": amount, "positionSide": "LONG",
        "entryPrice": entry_price, "leverage": "10",
    }


def test_monitor_picks_up_trader_from_copy_traders_json(isolated_store):
    """Ein in copy_traders.json abonnierter Trader wird ohne Bot-Start erkannt."""
    _write_subscribed_trader(isolated_store)

    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)

    assert monitor.is_following(WALLET) is True
    assert monitor.copy_size(WALLET) == 30.0
    assert monitor.followed() == {WALLET: 30.0}


def test_monitor_ignores_trader_not_marked_is_copied(isolated_store):
    """is_copied=0 in copy_traders.json -> kein Copy-Trading fuer diesen Wallet."""
    isolated_store.write_text(json.dumps([{
        "wallet": WALLET, "size_usdt": 30.0, "is_copied": 0, "is_focus": 0,
        "note": "", "source": "dashboard", "account_usd": 0.0,
        "win_rate": 0.0, "trades": 0, "added_at": 0.0, "updated_at": 0.0,
    }]))

    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)

    assert monitor.is_following(WALLET) is False
    assert monitor.followed() == {}


def test_monitor_emits_open_long_signal_on_fresh_position(isolated_store, monkeypatch):
    """Neue Long-Position des abonnierten Traders -> COPY_OPEN_LONG-Signal."""
    _write_subscribed_trader(isolated_store)
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)

    stage = {"has_position": False}

    def fake_fetch_other_positions(uid, trade_type=bl.TRADE_TYPE):
        assert uid == WALLET
        return [_position_row()] if stage["has_position"] else []

    monkeypatch.setattr(bl, "fetch_other_positions", fake_fetch_other_positions)

    async def _run() -> bl.CopySignal:
        # Baseline-Poll (wie beim Bot-Start): noch keine Position, kein Signal.
        await monitor._adapter._poll_all_positions(emit_signals=False)
        assert monitor._adapter.signal_queue.empty()

        # Trader eroeffnet jetzt eine frische Long-Position.
        stage["has_position"] = True
        await monitor._adapter._poll_all_positions(emit_signals=True)

        agen = monitor.signals()
        return await asyncio.wait_for(agen.__anext__(), timeout=1.0)

    sig = asyncio.run(_run())

    assert sig.signal == "COPY_OPEN_LONG"
    assert sig.trader == WALLET
    assert sig.coin == "BTC"
    assert sig.symbol == "BTCUSDT"
    assert sig.size_usd == pytest.approx(50.0)


def test_monitor_ignores_position_below_min_copy_size(isolated_store, monkeypatch):
    """Positionswert unter min_copy_size_usd -> kein Signal."""
    _write_subscribed_trader(isolated_store)
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=1000.0)

    def fake_fetch_other_positions(uid, trade_type=bl.TRADE_TYPE):
        return [_position_row(amount="0.001", entry_price="50000")]  # $50 Notional

    monkeypatch.setattr(bl, "fetch_other_positions", fake_fetch_other_positions)

    async def _run() -> None:
        await monitor._adapter._poll_all_positions(emit_signals=False)
        await monitor._adapter._poll_all_positions(emit_signals=True)

    asyncio.run(_run())

    assert monitor._adapter.signal_queue.empty()
