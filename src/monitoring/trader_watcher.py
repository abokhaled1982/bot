"""
src/monitoring/trader_watcher.py — Ueberwachungs-Schnittstelle fuer Copy-Trader-Skripte.

Kapselt den Polling-Adapter (`src.adapters.binance_leaderboard.BinanceLeaderboardTrader`)
hinter einer schmalen, dokumentierten Schnittstelle, ueber die Copy-Trader-Skripte
(z.B. `live_copytrader.py`) mit der Ueberwachung interagieren. Die eigentliche
Poll-/Diff-Logik (wie oft abgefragt wird, wie ein Positions-Open erkannt wird)
bleibt unveraendert in `src.adapters.binance_leaderboard` — dieses Modul legt
nur fest, WIE ein Consumer damit spricht.

Verantwortlichkeiten von `CopyTraderMonitor`
---------------------------------------------
  * Hintergrund-Ueberwachung starten: Trader-Liste alle 3s aus
    `data/copy_traders.json` neu einlesen, Positionen jedes abonnierten
    Traders alle `poll_interval` Sekunden abfragen, bei einer neu geoeffneten
    Long-Position ein `CopySignal(signal="COPY_OPEN_LONG", ...)` ausgeben.
  * Signale als async Generator durchreichen (`async for sig in monitor.signals()`).
  * Abo-Verwaltung: `follow` / `unfollow` / `is_following` / `copy_size` / `followed`.

Explizit NICHT Teil dieser Schnittstelle (bleibt beim Consumer):
  * Order-Ausfuehrung (Kauf/Verkauf)
  * Positions-Buchhaltung / Persistenz
  * Benachrichtigungen (WhatsApp o.ae.)
"""
from __future__ import annotations

from typing import AsyncIterator, Optional

from src.adapters.binance_leaderboard import BinanceLeaderboardTrader, CopySignal

__all__ = ["CopyTraderMonitor", "CopySignal"]


class CopyTraderMonitor:
    """Schmale Fassade um `BinanceLeaderboardTrader` fuer Copy-Trader-Skripte.

    Args:
        poll_interval: Sekunden zwischen zwei Positions-Abfragen je Trader.
        min_copy_size_usd: Positionen unterhalb dieses Notional-Werts (USD)
            erzeugen kein Copy-Signal.
    """

    def __init__(self, *, poll_interval: float, min_copy_size_usd: float) -> None:
        self._adapter = BinanceLeaderboardTrader(publish_state=False)
        self._adapter.set_poll_interval(poll_interval)
        self._adapter.set_min_copy_size(min_copy_size_usd)

    async def start(self) -> None:
        """Startet Store-Sync-, Poll- und Rescan-Loops — laeuft dauerhaft.

        In einem eigenen Task starten, z.B.:
            asyncio.create_task(monitor.start())
        """
        await self._adapter.start()

    async def signals(self) -> AsyncIterator[CopySignal]:
        """Endlos-Generator: liefert jedes neue Copy-Signal (wartet blockierend).

        Yields:
            CopySignal mit `signal` in {COPY_OPEN_LONG, COPY_CLOSE_LONG,
            COPY_INCREASE, COPY_DECREASE}.
        """
        while True:
            yield await self._adapter.signal_queue.get()

    def follow(self, trader_id: str, size_usdt: float) -> None:
        """Trader abonnieren — dessen naechste frische Long-Position wird kopiert."""
        self._adapter.activate_wallet(trader_id)
        self._adapter.set_copy_size(trader_id, size_usdt)

    def unfollow(self, trader_id: str) -> None:
        """Trader deabonnieren. Eine bereits offene Position laeuft unveraendert weiter."""
        self._adapter.deactivate_wallet(trader_id)

    def is_following(self, trader_id: str) -> bool:
        """True, wenn dieser Trader aktuell abonniert ist (Signale erzeugt)."""
        return self._adapter.is_copied(trader_id)

    def copy_size(self, trader_id: str) -> Optional[float]:
        """Konfigurierter Order-Betrag (USDT) fuer diesen Trader, falls gesetzt."""
        return self._adapter.get_copy_size(trader_id)

    def followed(self) -> dict[str, float]:
        """Alle abonnierten Trader mit ihrem jeweiligen Copy-Betrag (USDT)."""
        return self._adapter.get_followed()
