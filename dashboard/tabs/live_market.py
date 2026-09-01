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

        # ── Order Flow Signals (Top Section) ──────────────────────────────────────
        col_left, col_right = st.columns([3, 2])

        with col_left:
            st.markdown('<div class="section-header">🎯 Order Flow Kandidaten (Bot Fokus)</div>', unsafe_allow_html=True)
            st.caption("Coins mit Whale-Käufen UND Order Book Kaufdruck (Score ≥ 2)")

            if candidates:
                rows = []
                for c in candidates:
                    ch24  = c.get("change_24h", 0)
                    score = c.get("orderflow_score", 0)
                    sigs  = ", ".join([s.replace("WHALE_BUY", "🐋 BUY").replace("BOOK_LONG", "📗 L-BOOK") for s in c.get("signals", [])])
                    
                    rows.append({
                        "Symbol":    c["symbol"].replace("USDT", "/USDT"),
                        "Preis $":   f"${c['price_usd']:.6f}",
                        "Score":     score,
                        "Signale (Letzte 30s)": sigs,
                        "24h %":     ch24,
                        "Vol 24h":   c["volume_24h"] / 1e6,
                    })

                df = pd.DataFrame(rows)

                def _color_pct(v):
                    if isinstance(v, float):
                        if v > 0:  return "color: #00e6a7; font-weight: 700"
                        if v < 0:  return "color: #ff5c5c"
                    return "color: #64748b"

                styled = (
                    df.style
                    .format({"24h %": "{:+.2f}%", "Vol 24h": "${:.1f}M"})
                    .map(_color_pct, subset=["24h %"])
                    .set_properties(**{"background-color": "#0c0f16", "color": "#e2e8f0"})
                )
                st.dataframe(styled, use_container_width=True, height=250)
            else:
                st.info("Warte auf frische Whale Trades und Book Imbalances... (Signale verfallen nach 30s)")

        with col_right:
            st.markdown('<div class="section-header">🐋 Live Whale & Book Feed</div>', unsafe_allow_html=True)
            st.caption("Signale der letzten 30 Sekunden (Top 20 Paare)")
            
            if signals:
                feed_html = '<div style="max-height: 250px; overflow-y: auto; font-family: monospace; background: #0c0f16; padding: 10px; border-radius: 8px; border: 1px solid #1e2536;">'
                for sig in signals[:15]:
                    sym = sig.symbol.replace("USDT", "")
                    age = int(sig.age_sec)
                    
                    if sig.signal == "WHALE_BUY":
                        feed_html += f'<div style="color: #00e6a7; margin-bottom: 4px;">🐋 BUY  <b>{sym:6}</b>: ${sig.value_usd/1000:,.0f}k @ ${sig.price:.4f} <span style="color:#64748b; font-size: 0.8em; float: right;">{age}s ago</span></div>'
                    elif sig.signal == "WHALE_SELL":
                        feed_html += f'<div style="color: #ff5c5c; margin-bottom: 4px;">🐋 SELL <b>{sym:6}</b>: ${sig.value_usd/1000:,.0f}k @ ${sig.price:.4f} <span style="color:#64748b; font-size: 0.8em; float: right;">{age}s ago</span></div>'
                    elif sig.signal == "BOOK_LONG":
                        feed_html += f'<div style="color: #22c55e; margin-bottom: 4px;">📗 L-BOOK <b>{sym:6}</b>: {sig.ratio:.1f}x Bid/Ask <span style="color:#64748b; font-size: 0.8em; float: right;">{age}s ago</span></div>'
                    elif sig.signal == "BOOK_SHORT":
                        feed_html += f'<div style="color: #f87171; margin-bottom: 4px;">📕 S-BOOK <b>{sym:6}</b>: {sig.ratio:.2f}x Bid/Ask <span style="color:#64748b; font-size: 0.8em; float: right;">{age}s ago</span></div>'
                        
                feed_html += '</div>'
                st.markdown(feed_html, unsafe_allow_html=True)
            else:
                st.info("No fresh signals...")

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
                    use_container_width=True,
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
                    use_container_width=True,
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
            st.dataframe(styled_full, use_container_width=True, height=380)

    _render_live()
