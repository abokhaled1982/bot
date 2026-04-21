"""
dashboard/tabs/live_market.py — Binance WebSocket Live Market Tab

Zeigt Echtzeit-Binance-Daten (Order Flow, Whales, Imbalances) im Dashboard.
"""
import time
import threading
import asyncio

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


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
