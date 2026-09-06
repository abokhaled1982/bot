"""
Parsen und Ausfuehren von Text-Kommandos (WhatsApp / Konsole).

Der `CommandHandler` haelt keinen mutierbaren State selbst — er ruft
Callbacks in `live_copytrader.py` auf, die die eigentliche Order-Logik
und Persistenz uebernehmen.

Kommandos (fuehrendes `/` oder `!` optional, case-insensitive):

  help                             Diese Uebersicht
  status                           Balance, offene Positionen, PnL heute
  balance [ASSET]                  Binance-Guthaben (Default USDT)
  coins                            Alle gehaltenen Coins (nicht nur USDT)
  positions                        Offene Positionen auflisten
  traders [N]                      Top-N Trader nach realisiertem PnL
  trader <ID>                      Status eines Traders: Position + letzte Records
  price <COIN>                     Aktueller Marktpreis
  buy <COIN> <USDT>                Direkter Kauf, Position-Tag "MANUAL"
  sell <COIN>                      MANUAL-Position auf COIN schliessen
  close <COIN> [TRADER]            Schliesst Position(en) auf COIN
  copy <TRADER> <COIN> <USDT>      Kauf im Namen des Traders
  copyclose <TRADER> <COIN>        Schliesst Copy-Position dieses Traders
  follow <TRADER> <USDT>           Trader abonnieren (kopiert dessen naechste frische Position)
  unfollow <TRADER>                Trader deabonnieren
  following                        Abonnierte Trader + Betrag + Status
  stop                             touch STOP_BOT (blockt neue Opens)
  resume                           STOP_BOT entfernen
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional

from loguru import logger

STOP_FILE = "STOP_BOT"
QUOTE_ASSET = os.getenv("QUOTE_ASSET", "USDT").strip().upper() or "USDT"
SIM_STATS_FILE = os.getenv("SIM_STATS_FILE", "data/sim_trader_stats.json")

# Callback-Typen
OpenManualFn  = Callable[[str, str, float, str, float], str]  # (coin, sym, usdt, trader, wr) -> msg
ClosePosFn    = Callable[[str, Optional[str], str], str]      # (coin, trader?, reason) -> msg
PriceFn       = Callable[[str], Optional[float]]
BalanceFn     = Callable[[str], float]
AllBalancesFn = Callable[[], dict[str, float]]
TraderFocusFn = Callable[[str], Optional[dict]]
StateProvider = Callable[[], dict]
TradersProv   = Callable[[], dict]
FollowFn      = Callable[[str, float], str]   # (trader, usdt) -> msg
UnfollowFn    = Callable[[str], str]          # (trader) -> msg


HELP_TEXT = (
    "🤖 Copy-Trader — Kommandos:\n"
    "  /status                        Balance, offene Positionen, PnL heute\n"
    "  /balance [ASSET]               Guthaben (Default USDT)\n"
    "  /coins                         Alle gehaltenen Coins (nicht nur USDT)\n"
    "  /positions                     Offene Positionen\n"
    "  /traders [N]                   Top-N Trader nach PnL\n"
    "  /trader <ID>                   Status eines Traders (Position + Records)\n"
    "  /simtop [N]                    Top-N Sim-Trader (data/sim_trader_stats.json)\n"
    "  /price <COIN>                  Marktpreis\n"
    "  /buy <COIN> <USDT>             Direkter Kauf (Tag: MANUAL)\n"
    "  /sell <COIN>                   MANUAL-Position schliessen\n"
    "  /close <COIN> [TRADER]         Position schliessen\n"
    "  /copy <TRADER> <COIN> <USDT>   Kauf im Namen des Traders\n"
    "  /copyclose <TRADER> <COIN>     Copy-Position des Traders schliessen\n"
    "  /follow <TRADER> <USDT>        Trader abonnieren (naechste frische Position wird kopiert)\n"
    "  /unfollow <TRADER>             Trader deabonnieren\n"
    "  /following                     Abonnierte Trader + Betrag + Status\n"
    "  /stop                          Neue Opens blockieren\n"
    "  /resume                        Blockade aufheben\n"
    "  /help                          Diese Uebersicht"
)


def split_command(text: str) -> tuple[str, list[str]]:
    text = (text or "").strip()
    if not text:
        return "", []
    parts = text.split()
    head = parts[0].lstrip("/!").lower()
    return head, parts[1:]


def _symbol_for(coin: str) -> str:
    c = coin.upper()
    if c.endswith(("USDT", "BUSD", "USDC", "FDUSD")):
        return c
    return f"{c}{QUOTE_ASSET}"


def _short(uid: str, n: int = 8) -> str:
    return uid


class CommandHandler:
    def __init__(
        self, *,
        state_provider: StateProvider,
        traders_provider: TradersProv,
        open_manual: OpenManualFn,
        close_position: ClosePosFn,
        get_price: PriceFn,
        get_balance: BalanceFn,
        default_size_usdt: float,
        follow_trader: Optional[FollowFn] = None,
        unfollow_trader: Optional[UnfollowFn] = None,
        get_all_balances: Optional[AllBalancesFn] = None,
        trader_focus: Optional[TraderFocusFn] = None,
    ) -> None:
        self._state = state_provider
        self._traders = traders_provider
        self._open = open_manual
        self._close = close_position
        self._price = get_price
        self._balance = get_balance
        self._default_size = float(default_size_usdt)
        self._follow = follow_trader
        self._unfollow = unfollow_trader
        self._all_balances = get_all_balances
        self._trader_focus = trader_focus

    # ── Dispatcher ────────────────────────────────────────────────────────────
    def dispatch(self, text: str, source: str = "console") -> str:
        cmd, argv = split_command(text)
        if not cmd:
            return ""
        logger.info(f"[CMD:{source}] {text!r}")
        method = getattr(self, f"cmd_{cmd}", None)
        if not method:
            return f"❓ unbekannt: /{cmd}\n\n{HELP_TEXT}"
        try:
            return method(argv) or ""
        except Exception as e:
            logger.exception(f"[CMD] {cmd} Fehler")
            return f"❌ Fehler /{cmd}: {e}"

    # ── Kommandos ─────────────────────────────────────────────────────────────
    def cmd_help(self, _argv: list[str]) -> str:
        return HELP_TEXT

    def cmd_status(self, _argv: list[str]) -> str:
        st = self._state()
        bal = self._balance(QUOTE_ASSET)
        open_cnt = len(st.get("positions", []))
        hist = st.get("history", [])
        pnl_today = _daily_pnl(hist)
        pnl_total = sum(float(h.get("pnl_usdt") or 0) for h in hist)
        traders = self._traders()
        stop_active = os.path.exists(STOP_FILE)
        gate = "⛔ STOP_BOT aktiv" if stop_active else "✅ aktiv"
        return (
            f"📊 Status  ({gate})\n"
            f"Balance:  ${bal:.2f} {QUOTE_ASSET}\n"
            f"Offen:    {open_cnt}\n"
            f"Historie: {len(hist)}\n"
            f"PnL heute:  {pnl_today:+.2f} USDT\n"
            f"PnL gesamt: {pnl_total:+.2f} USDT\n"
            f"Abonnierte Trader: {len(traders)}\n"
            f"{self._following_lines(traders, st.get('positions', []))}"
        )

    def cmd_balance(self, argv: list[str]) -> str:
        asset = (argv[0].upper() if argv else QUOTE_ASSET)
        bal = self._balance(asset)
        return f"💰 {asset}: {bal:.6f}"

    def cmd_coins(self, _argv: list[str]) -> str:
        if self._all_balances is None:
            return "❌ /coins ist in diesem Modus nicht verfuegbar."
        balances = self._all_balances()
        if not balances:
            return "📭 keine Guthaben gefunden"
        lines = ["💰 Gehaltene Coins:"]
        for asset, amount in sorted(balances.items(), key=lambda kv: kv[0]):
            lines.append(f"  · {asset}: {amount:.6f}")
        return "\n".join(lines)

    def cmd_positions(self, _argv: list[str]) -> str:
        positions = self._state().get("positions", [])
        if not positions:
            return "📭 keine offenen Positionen"
        lines = ["📈 Offene Positionen:"]
        for p in positions:
            coin = p.get("coin", "?")
            uid = _short(p.get("trader_id") or "MANUAL")
            entry = float(p.get("entry_price") or 0)
            size = float(p.get("size_usdt") or 0)
            qty = float(p.get("qty") or 0)
            live = self._price(p.get("symbol") or _symbol_for(coin))
            pnl_str = ""
            if live and entry > 0:
                pnl_usdt = (live - entry) * qty
                pnl_pct = (live / entry - 1) * 100
                pnl_str = f" | uPnL {pnl_usdt:+.2f}$ ({pnl_pct:+.2f}%)"
            lines.append(
                f"  · {coin} [{uid}]  ${size:.2f}  @ ${entry:.6f}{pnl_str}"
            )
        return "\n".join(lines)

    def cmd_traders(self, argv: list[str]) -> str:
        try:
            n = int(argv[0]) if argv else 5
        except ValueError:
            n = 5
        hist = self._state().get("history", [])
        agg: dict[str, dict] = {}
        for h in hist:
            uid = str(h.get("trader_id") or "")
            if not uid:
                continue
            row = agg.setdefault(uid, {"uid": uid, "n": 0, "w": 0, "pnl": 0.0})
            row["n"] += 1
            pnl = float(h.get("pnl_usdt") or 0)
            row["pnl"] += pnl
            if pnl > 0:
                row["w"] += 1
        if not agg:
            return "📊 keine geschlossenen Trades"
        top = sorted(agg.values(), key=lambda x: x["pnl"], reverse=True)[:n]
        lines = [f"📊 Top {len(top)} Trader nach realisiertem PnL:"]
        for row in top:
            wr = 100 * row["w"] / row["n"] if row["n"] else 0
            lines.append(
                f"  · {_short(row['uid'])}  {row['pnl']:+.2f}$  "
                f"({row['n']}t, {wr:.0f}% wins)"
            )
        return "\n".join(lines)

    def cmd_simtop(self, argv: list[str]) -> str:
        try:
            n = int(argv[0]) if argv else 5
        except ValueError:
            n = 5
        n = max(1, min(n, 50))
        try:
            with open(SIM_STATS_FILE, encoding="utf-8") as fh:
                rows = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
            return f"❌ Sim-Stats nicht lesbar ({SIM_STATS_FILE}): {e}"
        if not isinstance(rows, list) or not rows:
            return "📊 Sim-Stats leer"
        rows = sorted(
            (r for r in rows if isinstance(r, dict)),
            key=lambda r: float(r.get("pnl_usdt") or 0), reverse=True,
        )[:n]
        lines = [f"🏆 Sim-Top {len(rows)} (nach PnL USDT):"]
        for i, r in enumerate(rows, 1):
            uid = str(r.get("trader_id") or "")
            pnl = float(r.get("pnl_usdt") or 0)
            wr  = float(r.get("win_rate") or 0)
            tr  = int(r.get("trades") or 0)
            v   = str(r.get("verdict") or "")
            lines.append(
                f'  {i}. trader_id: "{uid}"  {pnl:+.2f}$  '
                f"WR {wr:.0f}%  ({tr}t)  [{v}]"
            )
        lines.append("→ zum Kopieren: /follow <TRADER_ID> <USDT>")
        return "\n".join(lines)

    def cmd_trader(self, argv: list[str]) -> str:
        if not argv:
            return "Nutzung: /trader <TRADER_ID>"
        uid = argv[0].strip()
        st = self._state()
        followed = self._traders()
        lines = [f"👤 Trader {_short(uid)}"]
        if uid in followed:
            lines.append(f"Abonniert: ${float(followed[uid]):.2f}/Trade")

        # Echte Binance-Live-Positionen (aus dem gepollten Leaderboard, nicht unsere Copy-Trades)
        focus = self._trader_focus(uid) if self._trader_focus else None
        if focus is None:
            lines.append("⚠️ Binance-Live-Daten nicht verfuegbar (Adapter nicht verdrahtet)")
        else:
            bn_positions = focus.get("positions") or {}
            if not bn_positions:
                lines.append("Binance-Position: keine offene")
            else:
                lines.append("Binance-Position (live, aus Leaderboard-Polling):")
                for coin, p in bn_positions.items():
                    size = float(p.get("size") or 0)
                    entry = float(p.get("entry_px") or 0)
                    lev = float(p.get("leverage") or 1)
                    pnl_pct = float(p.get("pnl_pct") or 0)
                    upnl = float(p.get("unrealized_pnl") or 0)
                    value = float(p.get("value_usd") or 0)
                    side = "LONG" if size >= 0 else "SHORT"
                    lines.append(
                        f"  · {coin} {side}  ${value:.2f}  @ ${entry:.6f}  "
                        f"{lev:.0f}x  uPnL {upnl:+.2f}$ ({pnl_pct:+.2f}%)"
                    )

        # Unsere eigenen Copy-Trades (lokale Historie)
        positions = [p for p in st.get("positions", []) if p.get("trader_id") == uid]
        history = [h for h in st.get("history", []) if h.get("trader_id") == uid]
        if positions:
            lines.append("Unsere Copy-Position(en):")
            for p in positions:
                coin = p.get("coin", "?")
                entry = float(p.get("entry_price") or 0)
                size = float(p.get("size_usdt") or 0)
                qty = float(p.get("qty") or 0)
                live = self._price(p.get("symbol") or _symbol_for(coin))
                pnl_str = ""
                if live and entry > 0:
                    pnl_usdt = (live - entry) * qty
                    pnl_pct = (live / entry - 1) * 100
                    pnl_str = f" | uPnL {pnl_usdt:+.2f}$ ({pnl_pct:+.2f}%)"
                lines.append(f"  · {coin}  ${size:.2f}  @ ${entry:.6f}{pnl_str}")
        last = sorted(history, key=lambda h: h.get("closed_at_iso") or "", reverse=True)[:5]
        if last:
            lines.append("Unsere letzten Copy-Records:")
            for h in last:
                coin = h.get("coin", "?")
                pnl = float(h.get("pnl_usdt") or 0)
                closed = h.get("closed_at_iso") or "?"
                lines.append(f"  · {coin}  {pnl:+.2f}$  ({closed})")
        return "\n".join(lines)

    def cmd_price(self, argv: list[str]) -> str:
        if not argv:
            return "Nutzung: /price <COIN>"
        coin = argv[0].upper()
        sym = _symbol_for(coin)
        p = self._price(sym)
        if p is None:
            return f"⚠️ Preis {sym} nicht abrufbar"
        return f"💱 {sym}: ${p:.6f}"

    # ── Trading ───────────────────────────────────────────────────────────────
    def cmd_buy(self, argv: list[str]) -> str:
        if not argv:
            return "Nutzung: /buy <COIN> [USDT]"
        coin = argv[0].upper()
        usdt = _parse_amount(argv[1]) if len(argv) > 1 else self._default_size
        if usdt is None or usdt <= 0:
            return "Betrag ungueltig — Nutzung: /buy <COIN> <USDT>"
        return self._open(coin, _symbol_for(coin), usdt, "MANUAL", 0.0)

    def cmd_sell(self, argv: list[str]) -> str:
        if not argv:
            return "Nutzung: /sell <COIN>"
        coin = argv[0].upper()
        return self._close(coin, "MANUAL", "MANUAL_SELL")

    def cmd_close(self, argv: list[str]) -> str:
        if not argv:
            return "Nutzung: /close <COIN> [TRADER]"
        coin = argv[0].upper()
        trader = argv[1] if len(argv) > 1 else None
        return self._close(coin, trader, "MANUAL_CLOSE")

    def cmd_copy(self, argv: list[str]) -> str:
        if len(argv) < 3:
            return "Nutzung: /copy <TRADER> <COIN> <USDT>"
        trader = argv[0]
        coin = argv[1].upper()
        usdt = _parse_amount(argv[2])
        if usdt is None or usdt <= 0:
            return "Betrag ungueltig"
        return self._open(coin, _symbol_for(coin), usdt, trader, 0.0)

    def cmd_copyclose(self, argv: list[str]) -> str:
        if len(argv) < 2:
            return "Nutzung: /copyclose <TRADER> <COIN>"
        trader = argv[0]
        coin = argv[1].upper()
        return self._close(coin, trader, "MANUAL_COPYCLOSE")

    # ── Trader-Abo ────────────────────────────────────────────────────────────
    def _following_lines(self, followed: dict, positions: list[dict]) -> str:
        if not followed:
            return "  (keine abonnierten Trader)"
        lines = []
        for uid, size in followed.items():
            has_pos = any(p.get("trader_id") == uid for p in positions)
            status = "🟢 offene Position" if has_pos else "⚪ wartet auf frische Position"
            lines.append(f"  · {_short(uid)}  ${float(size):.2f}/Trade  {status}")
        return "\n".join(lines)

    def cmd_follow(self, argv: list[str]) -> str:
        if self._follow is None:
            return "❌ /follow ist in diesem Modus nicht verfuegbar."
        if len(argv) < 2:
            return "Nutzung: /follow <TRADER_ID> <BETRAG_USDT>"
        trader = argv[0].strip()
        usdt = _parse_amount(argv[1])
        if usdt is None or usdt <= 0:
            return "Betrag ungueltig — Nutzung: /follow <TRADER_ID> <BETRAG_USDT>"
        return self._follow(trader, usdt)

    def cmd_unfollow(self, argv: list[str]) -> str:
        if self._unfollow is None:
            return "❌ /unfollow ist in diesem Modus nicht verfuegbar."
        if not argv:
            return "Nutzung: /unfollow <TRADER_ID>"
        return self._unfollow(argv[0].strip())

    def cmd_following(self, _argv: list[str]) -> str:
        followed = self._traders()
        if not followed:
            return "📭 keine abonnierten Trader — /follow <TRADER_ID> <USDT>"
        positions = self._state().get("positions", [])
        return "👥 Abonnierte Trader:\n" + self._following_lines(followed, positions)

    # ── Kill-Switch ───────────────────────────────────────────────────────────
    def cmd_stop(self, _argv: list[str]) -> str:
        try:
            with open(STOP_FILE, "w", encoding="utf-8") as f:
                f.write("stopped by command\n")
            return f"⛔ STOP_BOT gesetzt — neue Opens werden geblockt."
        except OSError as e:
            return f"❌ STOP_BOT konnte nicht gesetzt werden: {e}"

    def cmd_resume(self, _argv: list[str]) -> str:
        try:
            if os.path.exists(STOP_FILE):
                os.remove(STOP_FILE)
                return "✅ STOP_BOT entfernt — Opens wieder erlaubt."
            return "✅ STOP_BOT war bereits nicht gesetzt."
        except OSError as e:
            return f"❌ STOP_BOT konnte nicht entfernt werden: {e}"


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────
def _parse_amount(text: str) -> Optional[float]:
    try:
        v = float(text.replace(",", ".").rstrip("$").rstrip("USD").rstrip("USDT"))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _daily_pnl(history: list[dict]) -> float:
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).astimezone().date()
    total = 0.0
    for h in history:
        try:
            d = datetime.fromisoformat(h.get("closed_at_iso") or "").astimezone().date()
        except Exception:
            continue
        if d == today:
            total += float(h.get("pnl_usdt") or 0)
    return total
