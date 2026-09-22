"""
tests/test_copy_flow.py — Beweis fuer die komplette Auto-Copy-Kette, ohne dass
ein Bot-Prozess laeuft und ohne echte Orders.

Abgedeckte Kette (identisch zum Live-Betrieb):
  WhatsApp `/follow <ID>`  --> CommandHandler --> CopyTraderMonitor (Abo)
  Trader oeffnet Position  --> COPY_OPEN_LONG --> _signal_loop --> market BUY
  Trader schliesst         --> COPY_CLOSE_LONG --> _signal_loop --> market SELL + PnL

Binance ist an zwei Stellen gemockt:
  * `fetch_other_positions` (was der Trader tut)
  * `buy_and_protect` / `market_sell` / `get_account_balance` (unsere Orders)
Der Trader-Store (copy_traders.json + SQLite) zeigt auf tmp_path.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import threading
import time

import pytest

import live_copytrader as lc
from src.adapters import binance_leaderboard as bl
from src.commands import CommandHandler
from src.monitoring import CopyTraderMonitor
from src.utils import trader_store

WALLET = "5041579634804156928"
OTHER_WALLET = "1111111111111111111"

# Der Bot handelt immer in QUOTE_ASSET (.env) — egal welches Paar der Trader nutzt.
BTC_SYMBOL = lc._force_quote("BTCUSDT")
ETH_SYMBOL = lc._force_quote("ETHUSDT")


# ── Fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """copy_traders.json + SQLite-DB des Trader-Stores auf tmp_path umleiten."""
    monkeypatch.setattr(trader_store, "TRADERS_FILE", str(tmp_path / "copy_traders.json"))
    monkeypatch.setattr(trader_store, "DB_PATH", str(tmp_path / "test.db"))


class FakeExchange:
    """Minimaler Spot-Markt: merkt sich Preise und protokolliert jede Order."""

    def __init__(self) -> None:
        self.prices: dict[str, float] = {BTC_SYMBOL: 50_000.0, ETH_SYMBOL: 2_000.0}
        self.buys: list[dict] = []
        self.sells: list[dict] = []
        self.buy_ok = True
        self.sell_ok = True
        self.balance = 10_000.0
        self.holdings: dict[str, float] = {}   # Base-Asset -> wirklich gehaltene Menge
        self.locked: dict[str, float] = {}     # davon im OCO-Exit gesperrt
        self.canceled: list[str] = []

    def _free(self, base: str) -> float:
        return self.holdings.get(base, 0.0) - self.locked.get(base, 0.0)

    def cancel_open_orders(self, symbol):
        self.canceled.append(symbol)
        return 1 if self.locked.pop(lc.info_base(symbol), 0.0) else 0

    def buy_and_protect(self, symbol, usdt_amount, trader="", coin="", price_hint=None):
        price = self.prices[symbol]
        self.buys.append({"symbol": symbol, "usdt": usdt_amount,
                          "trader": trader, "coin": coin, "price": price})
        if not self.buy_ok:
            return lc.ex.ExecutionResult(
                ok=False, action="BUY", symbol=symbol,
                reason="insufficient balance", error_code=-2010,
            ), None
        qty = usdt_amount / price
        base = lc.info_base(symbol)
        self.holdings[base] = self.holdings.get(base, 0.0) + qty
        self.locked[base] = self.locked.get(base, 0.0) + qty   # OCO sperrt den Bestand
        buy = lc.ex.ExecutionResult(
            ok=True, action="BUY", symbol=symbol, qty=qty, price=price,
            total_usdt=usdt_amount, order_id="B1", client_id="cb1", status="FILLED",
        )
        oco = lc.ex.ExecutionResult(ok=True, action="OCO", symbol=symbol,
                                    qty=qty, price=price, status="NEW")
        return buy, oco

    def market_sell(self, symbol, qty, trader="", coin=""):
        price = self.prices[symbol]
        base = lc.info_base(symbol)
        self.sells.append({"symbol": symbol, "qty": qty,
                           "trader": trader, "coin": coin, "price": price})
        if not self.sell_ok:
            return lc.ex.ExecutionResult(ok=False, action="SELL", symbol=symbol,
                                         reason="order rejected", error_code=-2010)
        if self.holdings and qty > self._free(base) + 1e-12:
            return lc.ex.ExecutionResult(
                ok=False, action="SELL", symbol=symbol,
                reason="Account has insufficient balance for requested action.",
                error_code=-2010,
            )
        if base in self.holdings:
            self.holdings[base] = max(0.0, self.holdings[base] - qty)
        return lc.ex.ExecutionResult(
            ok=True, action="SELL", symbol=symbol, qty=qty, price=price,
            total_usdt=qty * price, order_id="S1", client_id="cs1", status="FILLED",
        )

    def sell_all(self, symbol, trader="", coin=""):
        self.cancel_open_orders(symbol)
        qty = self._free(lc.info_base(symbol))
        if qty <= 0:
            return lc.ex.ExecutionResult(
                ok=False, action="SELL", symbol=symbol,
                reason=f"kein freier {lc.info_base(symbol)}-Bestand", error_code=-2010,
            )
        return self.market_sell(symbol, qty, trader=trader, coin=coin)


@pytest.fixture
def exchange(monkeypatch):
    fake = FakeExchange()
    monkeypatch.setattr(lc.ex, "DRY_RUN", True)
    monkeypatch.setattr(lc.ex, "buy_and_protect", fake.buy_and_protect)
    monkeypatch.setattr(lc.ex, "market_sell", fake.market_sell)
    monkeypatch.setattr(lc.ex, "sell_all", fake.sell_all)
    monkeypatch.setattr(lc.ex, "cancel_open_orders", fake.cancel_open_orders)
    monkeypatch.setattr(lc.ex, "get_account_balance", lambda asset="USDT": fake.balance)
    monkeypatch.setattr(lc.ex, "get_all_balances", lambda: dict(fake.holdings))
    monkeypatch.setattr(lc.ex, "get_price", lambda symbol: fake.prices.get(symbol))
    return fake


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, msg: str) -> None:
        self.messages.append(msg)

    def close(self) -> None:
        pass


@pytest.fixture
def bot(tmp_path, monkeypatch, isolated_store, exchange):
    """State + args + Notifier wie in `live_copytrader.run()`, aber auf tmp_path."""
    monkeypatch.setattr(lc, "STOP_FILE", str(tmp_path / "STOP_BOT"))
    args = argparse.Namespace(
        open_file=str(tmp_path / "open.json"),
        closed_file=str(tmp_path / "closed.json"),
        stats_file=str(tmp_path / "stats.json"),
        size_usdt=10.0,
        max_positions=5,
        max_daily_loss_usd=30.0,
        min_balance_usdt=1.0,
        usdt_eur_rate=0.92,
    )
    state = {"positions": [], "history": [], "lock": threading.RLock()}
    return argparse.Namespace(
        args=args, state=state, notifier=FakeNotifier(), exchange=exchange,
        tmp_path=tmp_path,
    )


# ── Helfer ────────────────────────────────────────────────────────────────────
def _position_row(symbol: str, amount: str, entry_price: str) -> dict:
    return {"symbol": symbol, "amount": amount, "positionSide": "LONG",
            "entryPrice": entry_price, "leverage": "10"}


class TraderFeed:
    """Was der beobachtete Trader gerade offen hat — pro Wallet steuerbar."""

    def __init__(self, monkeypatch) -> None:
        self.rows: dict[str, list[dict]] = {}
        monkeypatch.setattr(bl, "fetch_other_positions",
                            lambda uid, trade_type=bl.TRADE_TYPE: list(self.rows.get(uid, [])))

    def set(self, wallet: str, rows: list[dict]) -> None:
        self.rows[wallet] = rows


@pytest.fixture
def feed(monkeypatch):
    return TraderFeed(monkeypatch)


async def _drive(monitor, bot, *, until, timeout: float = 3.0) -> None:
    """`_signal_loop` laufen lassen, bis `until()` wahr ist (oder Timeout)."""
    task = asyncio.create_task(
        lc._signal_loop(monitor, bot.state, bot.args, bot.notifier)
    )
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and not until():
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _coins(bot) -> set[str]:
    return {p["coin"] for p in bot.state["positions"]}


# ── 1. Trader-Auswahl per WhatsApp ────────────────────────────────────────────
def test_follow_via_whatsapp_activates_auto_copy(isolated_store, bot):
    """`/follow <ID>` aus WhatsApp abonniert den Trader mit dem Default-Betrag."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    handler = CommandHandler(
        state_provider=lambda: bot.state,
        traders_provider=monitor.followed,
        open_manual=lambda *a: "",
        close_position=lambda *a: "",
        get_price=lambda s: 1.0,
        get_balance=lambda a: 0.0,
        default_size_usdt=bot.args.size_usdt,
        follow_trader=lambda t, u: lc._follow_trader(t, u, monitor=monitor),
        unfollow_trader=lambda t: lc._unfollow_trader(t, monitor=monitor),
    )

    reply = handler.dispatch(f"/follow {WALLET}", source="whatsapp")

    assert "auto-kopiert" in reply
    assert monitor.is_following(WALLET) is True
    assert monitor.copy_size(WALLET) == bot.args.size_usdt

    assert "10.00" in handler.dispatch("/following")

    assert "deabonniert" in handler.dispatch(f"/unfollow {WALLET}")
    assert monitor.is_following(WALLET) is False


def test_follow_with_explicit_amount_overrides_default(isolated_store, bot):
    """`/follow <ID> 25` setzt den Copy-Betrag fuer diesen Trader."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    handler = CommandHandler(
        state_provider=lambda: bot.state, traders_provider=monitor.followed,
        open_manual=lambda *a: "", close_position=lambda *a: "",
        get_price=lambda s: 1.0, get_balance=lambda a: 0.0,
        default_size_usdt=bot.args.size_usdt,
        follow_trader=lambda t, u: lc._follow_trader(t, u, monitor=monitor),
    )

    handler.dispatch(f"/follow {WALLET} 25")

    assert monitor.copy_size(WALLET) == 25.0


# ── 2. Signal-Erkennung -> Kauf ───────────────────────────────────────────────
def test_open_long_of_followed_trader_triggers_buy(isolated_store, bot, feed):
    """Frische Long-Position des Traders -> Market-BUY mit seinem Copy-Betrag."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    feed.set(WALLET, [])

    async def _run() -> None:
        await monitor._adapter._poll_all_positions(emit_signals=False)
        feed.set(WALLET, [_position_row("BTCUSDT", "0.01", "50000")])
        await monitor._adapter._poll_all_positions(emit_signals=True)
        await _drive(monitor, bot, until=lambda: bool(bot.state["positions"]))

    asyncio.run(_run())

    assert len(bot.exchange.buys) == 1
    buy = bot.exchange.buys[0]
    assert buy["symbol"] == BTC_SYMBOL
    assert buy["usdt"] == pytest.approx(10.0)
    assert buy["trader"] == WALLET

    pos = bot.state["positions"][0]
    assert pos["coin"] == "BTC"
    assert pos["trader_id"] == WALLET
    assert pos["size_usdt"] == pytest.approx(10.0)
    assert pos["qty"] == pytest.approx(10.0 / 50_000)

    # Persistenz + WhatsApp-Push
    assert json.loads(open(bot.args.open_file).read())[0]["coin"] == "BTC"
    assert any("OPEN LONG BTC" in m for m in bot.notifier.messages)


def test_every_coin_of_the_trader_is_copied(isolated_store, bot, feed):
    """Der Trader haelt mehrere Coins -> je Coin eine eigene Copy-Position."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    feed.set(WALLET, [])

    async def _run() -> None:
        await monitor._adapter._poll_all_positions(emit_signals=False)
        feed.set(WALLET, [
            _position_row("BTCUSDT", "0.01", "50000"),
            _position_row("ETHUSDT", "0.5", "2000"),
        ])
        await monitor._adapter._poll_all_positions(emit_signals=True)
        await _drive(monitor, bot, until=lambda: len(bot.state["positions"]) >= 2)

    asyncio.run(_run())

    assert _coins(bot) == {"BTC", "ETH"}
    assert len(bot.exchange.buys) == 2
    assert sum(b["usdt"] for b in bot.exchange.buys) == pytest.approx(20.0)


def test_same_coin_is_not_bought_twice(isolated_store, bot, feed):
    """Wiederholtes Signal auf denselben Coin erzeugt keine zweite Position."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    sig = bl.CopySignal(trader=WALLET, coin="BTC", symbol="BTCUSDT",
                        signal="COPY_OPEN_LONG", size_usd=500.0,
                        entry_price=50_000.0, leverage=10.0, pnl_pct=0.0)

    assert lc._handle_open(sig, bot.state, bot.args, bot.notifier, monitor) is True
    assert lc._handle_open(sig, bot.state, bot.args, bot.notifier, monitor) is False

    assert len(bot.state["positions"]) == 1
    assert len(bot.exchange.buys) == 1


def test_no_buy_for_unfollowed_trader(isolated_store, bot, feed):
    """Nicht abonnierter Trader -> kein Signal, kein Kauf."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    monitor.unfollow(WALLET)
    feed.set(WALLET, [])

    async def _run() -> None:
        await monitor._adapter._poll_all_positions(emit_signals=False)
        feed.set(WALLET, [_position_row("BTCUSDT", "0.01", "50000")])
        await monitor._adapter._poll_all_positions(emit_signals=True)
        await _drive(monitor, bot, until=lambda: False, timeout=0.3)

    asyncio.run(_run())

    assert bot.exchange.buys == []
    assert bot.state["positions"] == []


# ── 3. Signal-Erkennung -> Verkauf ────────────────────────────────────────────
def test_trader_close_triggers_sell_with_pnl(isolated_store, bot, feed):
    """Trader schliesst -> wir verkaufen exakt unsere Menge und buchen PnL."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    feed.set(WALLET, [])

    async def _run() -> None:
        await monitor._adapter._poll_all_positions(emit_signals=False)
        feed.set(WALLET, [_position_row("BTCUSDT", "0.01", "50000")])
        await monitor._adapter._poll_all_positions(emit_signals=True)
        await _drive(monitor, bot, until=lambda: bool(bot.state["positions"]))

        # +10% Marktbewegung, dann macht der Trader die Position zu.
        bot.exchange.prices[BTC_SYMBOL] = 55_000.0
        feed.set(WALLET, [])
        await monitor._adapter._poll_all_positions(emit_signals=True)
        await _drive(monitor, bot, until=lambda: bool(bot.state["history"]))

    asyncio.run(_run())

    assert len(bot.exchange.sells) == 1
    sell = bot.exchange.sells[0]
    assert sell["symbol"] == BTC_SYMBOL
    assert sell["qty"] == pytest.approx(10.0 / 50_000)

    assert bot.state["positions"] == []
    closed = bot.state["history"][0]
    assert closed["coin"] == "BTC"
    assert closed["close_reason"] == "TRADER_CLOSED"
    assert closed["pnl_usdt"] == pytest.approx(1.0, abs=1e-6)   # 10 USDT -> 11 USDT
    assert closed["pnl_pct"] == pytest.approx(10.0, abs=1e-6)
    assert closed["pnl_eur"] == pytest.approx(0.92, abs=1e-6)

    stats = json.loads(open(bot.args.stats_file).read())
    assert stats[0]["trader_id"] == WALLET
    assert stats[0]["verdict"] == "HELPFUL"


def test_close_only_affects_the_matching_coin(isolated_store, bot, feed):
    """Schliesst der Trader nur BTC, bleibt seine ETH-Copy offen."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    feed.set(WALLET, [])

    async def _run() -> None:
        await monitor._adapter._poll_all_positions(emit_signals=False)
        feed.set(WALLET, [
            _position_row("BTCUSDT", "0.01", "50000"),
            _position_row("ETHUSDT", "0.5", "2000"),
        ])
        await monitor._adapter._poll_all_positions(emit_signals=True)
        await _drive(monitor, bot, until=lambda: len(bot.state["positions"]) >= 2)

        feed.set(WALLET, [_position_row("ETHUSDT", "0.5", "2000")])
        await monitor._adapter._poll_all_positions(emit_signals=True)
        await _drive(monitor, bot, until=lambda: bool(bot.state["history"]))

    asyncio.run(_run())

    assert _coins(bot) == {"ETH"}
    assert [s["symbol"] for s in bot.exchange.sells] == [BTC_SYMBOL]


def test_close_signal_of_foreign_trader_is_ignored(isolated_store, bot):
    """Close-Signal eines anderen Traders schliesst unsere Position nicht."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    open_sig = bl.CopySignal(trader=WALLET, coin="BTC", symbol="BTCUSDT",
                             signal="COPY_OPEN_LONG", size_usd=500.0,
                             entry_price=50_000.0, leverage=10.0, pnl_pct=0.0)
    lc._handle_open(open_sig, bot.state, bot.args, bot.notifier, monitor)

    foreign = bl.CopySignal(trader=OTHER_WALLET, coin="BTC", symbol="BTCUSDT",
                            signal="COPY_CLOSE_LONG", size_usd=500.0,
                            entry_price=50_000.0, leverage=10.0, pnl_pct=0.0)

    assert lc._handle_close(foreign, bot.state, bot.args, bot.notifier) is False
    assert bot.exchange.sells == []
    assert len(bot.state["positions"]) == 1


def test_failed_sell_keeps_position_open(isolated_store, bot):
    """Fehlgeschlagener SELL -> Position bleibt offen, Warnung geht raus."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    sig = bl.CopySignal(trader=WALLET, coin="BTC", symbol="BTCUSDT",
                        signal="COPY_OPEN_LONG", size_usd=500.0,
                        entry_price=50_000.0, leverage=10.0, pnl_pct=0.0)
    lc._handle_open(sig, bot.state, bot.args, bot.notifier, monitor)
    bot.exchange.sell_ok = False

    close_sig = bl.CopySignal(trader=WALLET, coin="BTC", symbol="BTCUSDT",
                              signal="COPY_CLOSE_LONG", size_usd=500.0,
                              entry_price=50_000.0, leverage=10.0, pnl_pct=0.0)

    assert lc._handle_close(close_sig, bot.state, bot.args, bot.notifier) is False
    assert len(bot.state["positions"]) == 1
    assert any("CLOSE BTC fehlgeschlagen" in m for m in bot.notifier.messages)


# ── 4. Risiko-Gates ───────────────────────────────────────────────────────────
def test_failed_buy_creates_no_position(isolated_store, bot):
    """Order abgelehnt -> keine Phantom-Position, Warnung per WhatsApp."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    bot.exchange.buy_ok = False
    sig = bl.CopySignal(trader=WALLET, coin="BTC", symbol="BTCUSDT",
                        signal="COPY_OPEN_LONG", size_usd=500.0,
                        entry_price=50_000.0, leverage=10.0, pnl_pct=0.0)

    assert lc._handle_open(sig, bot.state, bot.args, bot.notifier, monitor) is False
    assert bot.state["positions"] == []
    assert any("OPEN BTC fehlgeschlagen" in m for m in bot.notifier.messages)


def test_stop_file_blocks_open_but_allows_close(isolated_store, bot):
    """STOP_BOT blockt neue Kaeufe — Verkaeufe laufen weiter."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    open_sig = bl.CopySignal(trader=WALLET, coin="BTC", symbol="BTCUSDT",
                             signal="COPY_OPEN_LONG", size_usd=500.0,
                             entry_price=50_000.0, leverage=10.0, pnl_pct=0.0)
    lc._handle_open(open_sig, bot.state, bot.args, bot.notifier, monitor)

    open(lc.STOP_FILE, "w").close()

    eth_sig = bl.CopySignal(trader=WALLET, coin="ETH", symbol="ETHUSDT",
                            signal="COPY_OPEN_LONG", size_usd=500.0,
                            entry_price=2_000.0, leverage=10.0, pnl_pct=0.0)
    assert lc._handle_open(eth_sig, bot.state, bot.args, bot.notifier, monitor) is False
    assert len(bot.exchange.buys) == 1

    close_sig = bl.CopySignal(trader=WALLET, coin="BTC", symbol="BTCUSDT",
                              signal="COPY_CLOSE_LONG", size_usd=500.0,
                              entry_price=50_000.0, leverage=10.0, pnl_pct=0.0)
    assert lc._handle_close(close_sig, bot.state, bot.args, bot.notifier) is True


def test_max_positions_gate_blocks_further_buys(isolated_store, bot):
    """Ist `--max-positions` erreicht, wird kein weiterer Coin gekauft."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    bot.args.max_positions = 1
    for coin, symbol, price in (("BTC", "BTCUSDT", 50_000.0), ("ETH", "ETHUSDT", 2_000.0)):
        sig = bl.CopySignal(trader=WALLET, coin=coin, symbol=symbol,
                            signal="COPY_OPEN_LONG", size_usd=500.0,
                            entry_price=price, leverage=10.0, pnl_pct=0.0)
        lc._handle_open(sig, bot.state, bot.args, bot.notifier, monitor)

    assert _coins(bot) == {"BTC"}
    assert any("max positions" in m for m in bot.notifier.messages)


def test_balance_gate_blocks_buy_when_live(isolated_store, bot, monkeypatch):
    """Live-Modus: zu wenig Guthaben -> kein Kauf."""
    monkeypatch.setattr(lc.ex, "DRY_RUN", False)
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    bot.exchange.balance = 2.0
    sig = bl.CopySignal(trader=WALLET, coin="BTC", symbol="BTCUSDT",
                        signal="COPY_OPEN_LONG", size_usd=500.0,
                        entry_price=50_000.0, leverage=10.0, pnl_pct=0.0)

    assert lc._handle_open(sig, bot.state, bot.args, bot.notifier, monitor) is False
    assert bot.exchange.buys == []
    assert any("Balance" in m for m in bot.notifier.messages)


def test_daily_loss_gate_blocks_buy(isolated_store, bot):
    """Ueberschrittener Tagesverlust -> keine neuen Kaeufe mehr."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    bot.state["history"].append({
        "trader_id": WALLET, "coin": "XRP", "pnl_usdt": -40.0, "pnl_eur": -36.8,
        "closed_at_iso": lc._now_iso(), "close_reason": "TRADER_CLOSED",
    })
    sig = bl.CopySignal(trader=WALLET, coin="BTC", symbol="BTCUSDT",
                        signal="COPY_OPEN_LONG", size_usd=500.0,
                        entry_price=50_000.0, leverage=10.0, pnl_pct=0.0)

    assert lc._handle_open(sig, bot.state, bot.args, bot.notifier, monitor) is False
    assert any("tagesloss" in m for m in bot.notifier.messages)


def test_short_position_of_trader_is_not_copied(isolated_store, bot, feed):
    """Wir handeln nur Spot-Long — SHORT des Traders erzeugt kein Copy-Signal."""
    monitor = CopyTraderMonitor(poll_interval=3.0, min_copy_size_usd=10.0)
    monitor.follow(WALLET, 10.0)
    feed.set(WALLET, [])

    async def _run() -> None:
        await monitor._adapter._poll_all_positions(emit_signals=False)
        feed.set(WALLET, [{"symbol": "BTCUSDT", "amount": "0.01",
                           "positionSide": "SHORT", "entryPrice": "50000",
                           "leverage": "10"}])
        await monitor._adapter._poll_all_positions(emit_signals=True)
        await _drive(monitor, bot, until=lambda: False, timeout=0.3)

    asyncio.run(_run())

    assert bot.exchange.buys == []
    assert bot.state["positions"] == []


# ── 5. Robustes Schliessen im Live-Modus ──────────────────────────────────────
def _book_position(bot, *, symbol: str, qty: float, size_usdt: float = 6.0) -> None:
    bot.state["positions"].append({
        "trader_id": WALLET, "trader_win_rate": 0.0, "coin": "BTC",
        "symbol": symbol, "side": "LONG", "size_usdt": size_usdt,
        "entry_price": size_usdt / qty, "entry_price_trader": 0.0, "qty": qty,
        "order_id": "B1", "client_id": "cb1",
        "opened_at": time.time(), "opened_at_iso": lc._now_iso(),
    })


CLOSE_SIG = bl.CopySignal(trader=WALLET, coin="BTC", symbol="BTCUSDT",
                          signal="COPY_CLOSE_LONG", size_usd=6.0,
                          entry_price=50_000.0, leverage=1.0, pnl_pct=0.0)


def test_legacy_usdt_position_is_sold_on_the_current_quote_pair(
    isolated_store, bot, monkeypatch,
):
    """Alte BTCUSDT-Position wird auf dem erlaubten Paar (QUOTE_ASSET) verkauft."""
    monkeypatch.setattr(lc.ex, "DRY_RUN", False)
    _book_position(bot, symbol="BTCUSDT", qty=0.00012)
    bot.exchange.holdings = {"BTC": 0.00012}

    assert lc._handle_close(CLOSE_SIG, bot.state, bot.args, bot.notifier) is True
    assert bot.exchange.sells[0]["symbol"] == BTC_SYMBOL
    assert bot.state["positions"] == []


def test_sell_is_capped_to_the_qty_actually_held(isolated_store, bot, monkeypatch):
    """Kaufgebuehr geht vom Coin ab -> es wird nur der reale Bestand verkauft."""
    monkeypatch.setattr(lc.ex, "DRY_RUN", False)
    _book_position(bot, symbol=BTC_SYMBOL, qty=0.00012)
    bot.exchange.holdings = {"BTC": 0.00011988}   # 0.1% Taker-Fee

    assert lc._handle_close(CLOSE_SIG, bot.state, bot.args, bot.notifier) is True
    assert bot.exchange.sells[0]["qty"] == pytest.approx(0.00011988)


def test_phantom_position_is_removed_instead_of_failing_forever(
    isolated_store, bot, monkeypatch,
):
    """Position im Buch, Coin nicht im Konto -> Buch bereinigen, keine Order."""
    monkeypatch.setattr(lc.ex, "DRY_RUN", False)
    _book_position(bot, symbol="BTCUSDT", qty=0.00037)
    bot.exchange.holdings = {"BTC": 0.00000699}   # Staub aus einem DRY_RUN-Lauf

    assert lc._handle_close(CLOSE_SIG, bot.state, bot.args, bot.notifier) is True
    assert bot.exchange.sells == []
    assert bot.state["positions"] == []
    assert bot.state["history"] == []
    assert any("nicht im Konto" in m for m in bot.notifier.messages)


def test_info_base_strips_usdc_quote(bot):
    """Base-Asset muss auch bei USDC-Paaren korrekt sein (BTCUSDC -> BTC)."""
    assert lc.info_base("BTCUSDC") == "BTC"
    assert lc.info_base("BTCUSDT") == "BTC"
    assert lc.info_base("ETHFDUSD") == "ETH"


# ── 6. Manuelles /buy und /sell ───────────────────────────────────────────────
def _manual_handler(bot) -> CommandHandler:
    return CommandHandler(
        state_provider=lambda: bot.state,
        traders_provider=dict,
        open_manual=lambda coin, sym, usdt, trader, wr: lc._open_manual(
            coin, sym, usdt, trader, wr,
            state=bot.state, args=bot.args, notifier=bot.notifier,
        ),
        close_position=lambda coin, trader, reason: lc._close_by_coin(
            coin, trader, reason,
            state=bot.state, args=bot.args, notifier=bot.notifier,
        ),
        get_price=lc._cmd_get_price,
        get_balance=lc._cmd_get_balance,
        default_size_usdt=bot.args.size_usdt,
        sell_all=lambda coin: lc._sell_all(
            coin, state=bot.state, args=bot.args, notifier=bot.notifier,
        ),
    )


def test_manual_buy_then_sell_round_trip(isolated_store, bot, monkeypatch):
    """/buy legt Position + OCO an, /sell loest den OCO und verkauft den Bestand."""
    monkeypatch.setattr(lc.ex, "DRY_RUN", False)
    handler = _manual_handler(bot)

    assert "OPEN LONG BTC" in handler.dispatch("/buy BTC 10")
    assert bot.exchange.buys[0]["symbol"] == BTC_SYMBOL
    assert bot.exchange.locked["BTC"] > 0          # OCO sperrt den Coin

    reply = handler.dispatch("/sell BTC")

    assert "SELL ALL BTC" in reply
    assert bot.exchange.canceled == [BTC_SYMBOL]   # OCO wurde vorher storniert
    assert bot.exchange.sells[0]["qty"] == pytest.approx(10.0 / 50_000.0)
    assert bot.exchange.holdings["BTC"] == pytest.approx(0.0)
    assert bot.state["positions"] == []
    assert bot.state["history"][-1]["close_reason"] == "MANUAL_SELL_ALL"


def test_sell_liquidates_coin_without_book_position(isolated_store, bot, monkeypatch):
    """/sell verkauft auch Restbestaende, die gar nicht im Buch stehen."""
    monkeypatch.setattr(lc.ex, "DRY_RUN", False)
    bot.exchange.holdings = {"BTC": 0.0005}
    handler = _manual_handler(bot)

    assert "SELL ALL BTC" in handler.dispatch("/sell BTC")
    assert bot.exchange.sells[0]["qty"] == pytest.approx(0.0005)
    assert bot.state["history"] == []


def test_close_cancels_oco_before_selling(isolated_store, bot, monkeypatch):
    """Copy-Close muss den OCO loesen — sonst ist der Coin gesperrt (-2010)."""
    monkeypatch.setattr(lc.ex, "DRY_RUN", False)
    _book_position(bot, symbol=BTC_SYMBOL, qty=0.0002)
    bot.exchange.holdings = {"BTC": 0.0002}
    bot.exchange.locked = {"BTC": 0.0002}

    assert lc._handle_close(CLOSE_SIG, bot.state, bot.args, bot.notifier) is True
    assert bot.exchange.canceled == [BTC_SYMBOL]
    assert bot.exchange.sells[0]["qty"] == pytest.approx(0.0002)
    assert bot.state["positions"] == []


def test_sell_reports_failure_when_nothing_is_held(isolated_store, bot, monkeypatch):
    """Ohne Bestand gibt /sell eine klare Fehlermeldung statt einer Order."""
    monkeypatch.setattr(lc.ex, "DRY_RUN", False)
    handler = _manual_handler(bot)

    reply = handler.dispatch("/sell BTC")

    assert reply.startswith("❌ SELL BTC fehlgeschlagen")
    assert bot.exchange.sells == []


def test_unsellable_dust_frees_the_position_slot(isolated_store, bot, monkeypatch):
    """Unter Binance-Minimum laesst sich nichts verkaufen -> Slot darf nicht blockiert bleiben."""
    monkeypatch.setattr(lc.ex, "DRY_RUN", False)
    _book_position(bot, symbol=BTC_SYMBOL, qty=0.0002)
    bot.exchange.holdings = {"BTC": 0.0002}
    monkeypatch.setattr(lc.ex, "market_sell", lambda *a, **k: lc.ex.ExecutionResult(
        ok=False, action="SELL", symbol=BTC_SYMBOL,
        reason="Staub: nur $3.00 wert, Binance-Minimum $5.00 (NOTIONAL)",
        error_code=lc.ex.DUST_BELOW_MINIMUM,
    ))

    assert lc._handle_close(CLOSE_SIG, bot.state, bot.args, bot.notifier) is True
    assert bot.state["positions"] == []
    assert any("Rest bleibt im Konto" in m for m in bot.notifier.messages)
