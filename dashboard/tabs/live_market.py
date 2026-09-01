"""
dashboard/tabs/live_market.py — Binance WebSocket Live Market Tab

Zeigt Echtzeit-Binance-Daten (Order Flow, Whales, Imbalances) im Dashboard.
"""
import asyncio
import json
import os
import sqlite3
import threading
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def _load_paper_positions() -> dict:
    try:
        with open("positions.json", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


def _load_paper_events() -> pd.DataFrame:
    try:
        conn = sqlite3.connect("binance_orderflow.db")
        events = pd.read_sql_query(
            """SELECT event_type, symbol, price_usd, buy_amount_usd, sell_amount_usd,
                      pnl_usd, pnl_pct, stage, message, timestamp
               FROM bot_events
               WHERE stage IN ('ORDERFLOW_EXECUTION', 'PAPER_EXIT')
               ORDER BY id DESC LIMIT 20""",
            conn,
        )
        conn.close()
        return events
    except (sqlite3.Error, OSError):
        return pd.DataFrame()


def _gate_badge(passed: bool) -> str:
    return "PASS" if passed else "STOP"


def _strategy_snapshot(adapter, tickers: dict, positions: dict) -> tuple[list[dict], dict | None]:
    from src.bot.orderflow_pipeline import (
        FLOW_WINDOW_SEC,
        MAX_POSITIONS,
        gate1_liquidity,
        gate2_whale_signal,
        gate3_book_pressure,
        gate4_flow_momentum,
    )

    rows = []
    for symbol in sorted(tickers, key=lambda item: tickers[item]["volume_24h"], reverse=True):
        ticker = tickers[symbol]
        metrics = adapter.get_flow_metrics(symbol, FLOW_WINDOW_SEC)
        if metrics["book_age_sec"] > 5:
            continue
        g1, g1_reason = gate1_liquidity(ticker, metrics)
        g2, g2_reason = gate2_whale_signal(symbol, adapter)
        g3, g3_reason = gate3_book_pressure(metrics)
        g4, g4_reason = gate4_flow_momentum(metrics)
        g5 = symbol not in positions and len(positions) < MAX_POSITIONS
        g5_reason = "Position frei" if g5 else "Position bereits offen oder Limit erreicht"
        checks = [g1, g2, g3, g4, g5]
        reasons = [g1_reason, g2_reason, g3_reason, g4_reason, g5_reason]
        first_stop = next((reason for passed, reason in zip(checks, reasons) if not passed), "Alle Gates bestanden")

        rows.append({
            "symbol": symbol,
            "price": ticker["price_usd"],
            "checks": checks,
            "reason": first_stop,
            "metrics": metrics,
            "approved": all(checks),
        })

        if len(rows) == 12:
            break

    best = max(rows, key=lambda row: (sum(row["checks"]), row["metrics"]["flow_ratio"]), default=None)
    return rows, best


@st.cache_resource
def _get_adapter():
    """Return (or create) the singleton BinanceOrderFlowAdapter."""
    from src.adapters.binance_orderflow import BinanceOrderFlowAdapter
    adapter = BinanceOrderFlowAdapter()

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(adapter.start())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return adapter


def render():
    from dashboard.config import C_GREEN, C_RED, C_MUTED

    @st.fragment(run_every="2s")
    def _render_live():
        adapter = _get_adapter()
        status  = adapter.status()

        # ── Header ────────────────────────────────────────────────────────────────
        c1, c2, c3, c4 = st.columns(4)

        dot_color = C_GREEN if status["connected"] else C_RED
        dot_label = "LIVE" if status["connected"] else "CONNECTING"
        age       = status.get("age_sec") or "—"

        with c1:
            st.markdown(f"""
            <div class="kpi-card">
              <div class="label">📡 Order Flow Streams</div>
              <div class="value" style="color:{dot_color};font-size:1.1rem">
                ● {dot_label}
              </div>
              <div class="sub">Last update: {age}s ago</div>
            </div>""", unsafe_allow_html=True)

        with c2:
            st.markdown(f"""
            <div class="kpi-card">
              <div class="label">📊 Top Pairs Watched</div>
              <div class="value">{status['subscribed_pairs']}</div>
              <div class="sub">Level 2 Data Active</div>
            </div>""", unsafe_allow_html=True)

        tickers = adapter.all_tickers()
        signals = adapter.get_signals()
        candidates = adapter.get_candidates(limit=15)

        if tickers:
            all_changes = [t["change_24h"] for t in tickers.values()]
            gainers = sum(1 for c in all_changes if c > 0)
            losers  = sum(1 for c in all_changes if c < 0)
            avg_ch  = sum(all_changes) / len(all_changes)

            with c3:
                col = C_GREEN if avg_ch > 0 else C_RED
                st.markdown(f"""
                <div class="kpi-card">
                  <div class="label">📈 Market Sentiment</div>
                  <div class="value" style="color:{col}">{avg_ch:+.2f}%</div>
                  <div class="sub">Ø 24h change (All USDT)</div>
                </div>""", unsafe_allow_html=True)

            with c4:
                st.markdown(f"""
                <div class="kpi-card">
                  <div class="label">🟢 Gainers / 🔴 Losers</div>
                  <div class="value">
                    <span style="color:{C_GREEN}">{gainers}</span>
                    <span style="color:{C_MUTED};font-size:1rem"> / </span>
                    <span style="color:{C_RED}">{losers}</span>
                  </div>
                  <div class="sub">24h basis</div>
                </div>""", unsafe_allow_html=True)
        else:
            with c3:
                st.markdown('<div class="kpi-card"><div class="label">Market</div><div class="value" style="color:#64748b">—</div></div>', unsafe_allow_html=True)
            with c4:
                st.markdown('<div class="kpi-card"><div class="label">Status</div><div class="value" style="color:#64748b">Waiting...</div></div>', unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        if not tickers:
            st.info("⏳ Verbinde mit Binance und warte auf Level 2 Daten... Bitte warten.")
            return

        # ── Trading decision ─────────────────────────────────────────────────────
        positions = _load_paper_positions()
        strategy_rows, best = _strategy_snapshot(adapter, tickers, positions)
        st.markdown('<div class="section-header">Aktuelle Trading-Entscheidung</div>', unsafe_allow_html=True)

        if best and best["approved"]:
            st.success(
                f"KAUFEN (nur Simulation): {best['symbol'].replace('USDT', '/USDT')} besteht alle fuenf Regeln."
            )
        elif best:
            st.warning(
                f"WARTEN: Das beste beobachtete Paar ist {best['symbol'].replace('USDT', '/USDT')}. "
                f"Noch kein Kauf, weil: {best['reason']}"
            )
        else:
            st.info("Warte auf genug Orderbook- und Trade-Daten fuer eine Entscheidung.")

        decision_rows = []
        for row in strategy_rows:
            metrics = row["metrics"]
            decision_rows.append({
                "Paar": row["symbol"].replace("USDT", "/USDT"),
                "Entscheidung": "KAUFEN" if row["approved"] else "WARTEN",
                "G1 Markt": _gate_badge(row["checks"][0]),
                "G2 Whale": _gate_badge(row["checks"][1]),
                "G3 Buch": _gate_badge(row["checks"][2]),
                "G4 Flow": _gate_badge(row["checks"][3]),
                "G5 Risiko": _gate_badge(row["checks"][4]),
                "Warum warten?": "-" if row["approved"] else row["reason"],
                "Flow": metrics["flow_ratio"],
                "Momentum": metrics["momentum_pct"],
                "Spread": metrics["spread_bps"],
                "Wand weg": metrics["wall_pull_pct"],
                "Absorption": "JA" if metrics.get("absorbed") else "-",
            })

        if decision_rows:
            decisions = pd.DataFrame(decision_rows)
            st.dataframe(
                decisions.style.format({
                    "Flow": "{:.2f}x", "Momentum": "{:+.3f}%",
                    "Spread": "{:.1f} bps", "Wand weg": "-{:.0f}%",
                }),
                width="stretch",
                hide_index=True,
                height=360,
            )

        with st.expander("Was bedeuten die Regeln?", expanded=False):
            st.markdown(
                "**G1 Markt:** Das Paar muss handelbar sein: enger Spread, genug Buchtiefe und kein starker Tagescrash.  \n"
                "**G2 Whale:** Ein grosser aggressiver Kauf ist gerade passiert.  \n"
                "**G3 Buch:** Die Kaufseite des Orderbooks war nicht nur einmal, sondern wiederholt staerker "
                "und die grossen Kauforders sind nicht ploetzlich verschwunden.  \n"
                "**G4 Flow:** In den letzten 30 Sekunden ueberwiegen Marktkaeufe und der Preis steigt leicht.  \n"
                "**G5 Risiko:** Es gibt noch Platz im Positionslimit und das Paar ist noch nicht offen.  \n\n"
                "**Wand weg:** Wie stark die groesste Kaufmauer geschrumpft ist. Viel Schwund deutet auf Koeder hin.  \n"
                "**Absorption:** Verkaeufer druecken in den Markt, aber der Kurs haelt — jemand kauft still auf."
            )

        if signals:
            st.caption("Letzte Marktsignale: " + " | ".join(
                f"{signal.symbol.replace('USDT', '/USDT')} {signal.signal} ({int(signal.age_sec)}s)"
                for signal in signals[:6]
            ))

        # ── Paper trading simulation ───────────────────────────────────────────
        st.markdown("---")
        st.markdown('<div class="section-header">Paper Trading Simulation</div>', unsafe_allow_html=True)
        positions = _load_paper_positions()
        take_profit_pct = float(os.getenv("BINANCE_TAKE_PROFIT_PCT", "1.5"))
        stop_loss_pct = float(os.getenv("BINANCE_STOP_LOSS_PCT", "2.0"))
        paper_positions = {
            symbol: position for symbol, position in positions.items() if position.get("dry_run")
        }
        events = _load_paper_events()
        completed_sells = events[events["stage"] == "PAPER_EXIT"] if not events.empty else pd.DataFrame()
        realized_pnl = completed_sells["pnl_usd"].fillna(0).sum() if not completed_sells.empty else 0.0

        metric_open, metric_invested, metric_closed, metric_pnl = st.columns(4)
        metric_open.metric("Offene Paper-Positionen", len(paper_positions))
        metric_invested.metric(
            "Aktuell investiert",
            f"${sum(float(position.get('size_usdt', 0)) for position in paper_positions.values()):,.2f}",
        )
        metric_closed.metric("Simulierte Verkaeufe", len(completed_sells))
        metric_pnl.metric("Realisierter PnL", f"${realized_pnl:+,.2f}")

        simulation_left, simulation_right = st.columns(2)
        with simulation_left:
            st.markdown("**Offene simulierte Positionen**")
            position_rows = []
            for symbol, position in paper_positions.items():
                entry_price = float(position.get("entry_price", 0))
                current_price = float(tickers.get(symbol, {}).get("price_usd", entry_price))
                pnl_pct = ((current_price / entry_price) - 1) * 100 if entry_price else 0
                position_rows.append({
                    "Paar": symbol.replace("USDT", "/USDT"),
                    "Einstieg": entry_price,
                    "Aktuell": current_price,
                    "PnL %": pnl_pct,
                    "TP": entry_price * (1 + take_profit_pct / 100),
                    "SL": entry_price * (1 - stop_loss_pct / 100),
                })
            if position_rows:
                st.dataframe(
                    pd.DataFrame(position_rows).style.format({
                        "Einstieg": "${:.6f}", "Aktuell": "${:.6f}", "PnL %": "{:+.2f}%",
                        "TP": "${:.6f}", "SL": "${:.6f}",
                    }),
                    width="stretch",
                    hide_index=True,
                )
            else:
                st.info("Noch keine offene Paper-Position.")

        with simulation_right:
            st.markdown("**Letzte simulierte Ausfuehrungen**")
            if events.empty:
                st.info("Noch keine Simulation. Ein Kauf erscheint nach einem vollstaendigen Orderflow-Signal.")
            else:
                st.dataframe(
                    events.assign(Paar=events["symbol"].str.replace("USDT", "/USDT", regex=False))[
                        ["timestamp", "event_type", "Paar", "price_usd", "pnl_usd", "pnl_pct", "message"]
                    ].rename(columns={
                        "timestamp": "Zeit", "event_type": "Aktion", "price_usd": "Preis",
                        "pnl_usd": "PnL $", "pnl_pct": "PnL %", "message": "Grund",
                    }).style.format({"Preis": "${:.6f}", "PnL $": "${:+.2f}", "PnL %": "{:+.2f}%"}),
                    width="stretch",
                    hide_index=True,
                )

        # ── Full market table ─────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown('<div class="section-header">📋 Alle USDT Märkte</div>', unsafe_allow_html=True)

        search = st.text_input("🔍 Symbol suchen", "", placeholder="z.B. BTC, ETH, SOL", key="binance_search")

        rows = []
        for sym, td in sorted(tickers.items()):
            if search and search.upper() not in sym:
                continue
            rows.append({
                "Symbol":   sym.replace("USDT", "/USDT"),
                "Preis $":  td["price_usd"],
                "24h %":    td["change_24h"],
                "Hoch 24h": td["high_24h"],
                "Tief 24h": td["low_24h"],
                "Vol 24h $M": td["volume_24h"] / 1e6,
            })

        if rows:
            full_df = pd.DataFrame(rows)
            styled_full = (
                full_df.style
                .format({
                    "Preis $":    "${:.6f}",
                    "24h %":      "{:+.2f}%",
                    "Hoch 24h":   "${:.6f}",
                    "Tief 24h":   "${:.6f}",
                    "Vol 24h $M": "${:.1f}M",
                })
                .map(lambda v: ("color: #00e6a7; font-weight:700" if isinstance(v, float) and v > 0
                                else ("color: #ff5c5c" if isinstance(v, float) and v < 0 else "")),
                     subset=["24h %"])
                .set_properties(**{"background-color": "#0c0f16", "color": "#e2e8f0"})
            )
            st.dataframe(styled_full, width="stretch", height=380)

    _render_live()
