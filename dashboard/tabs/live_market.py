"""
dashboard/tabs/live_market.py — Binance WebSocket Live Market Tab

Zeigt Echtzeit-Binance-Daten direkt im Haupt-Dashboard.
"""
import time
import threading
import asyncio

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


@st.cache_resource
def _get_adapter():
    """Return (or create) the singleton BinanceStreamAdapter."""
    from src.adapters.binance_stream import BinanceStreamAdapter
    adapter = BinanceStreamAdapter()

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(adapter.start())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return adapter


def render():
    from dashboard.config import DASHBOARD_CSS, C_GREEN, C_RED, C_MUTED

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
          <div class="label">📡 Binance WebSocket</div>
          <div class="value" style="color:{dot_color};font-size:1.1rem">
            ● {dot_label}
          </div>
          <div class="sub">Last update: {age}s ago</div>
        </div>""", unsafe_allow_html=True)

    with c2:
        st.markdown(f"""
        <div class="kpi-card">
          <div class="label">📊 Tracked Pairs</div>
          <div class="value">{status['tracked_symbols']}</div>
          <div class="sub">USDT Spot Pairs</div>
        </div>""", unsafe_allow_html=True)

    tickers = adapter.all_tickers()
    candidates = adapter.get_candidates(limit=20)

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
              <div class="sub">Ø 24h change</div>
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
        st.info("⏳ Verbinde mit Binance... Bitte 2–3 Sekunden warten.")
        time.sleep(1)
        st.rerun()
        return

    # ── Two-column layout ─────────────────────────────────────────────────────
    left, right = st.columns([3, 2])

    with left:
        st.markdown('<div class="section-header">🎯 Top Scanner Kandidaten</div>', unsafe_allow_html=True)
        st.caption("Coins mit höchstem Momentum-Score — sortiert nach Volume-Spike + Preis-Momentum")

        if candidates:
            rows = []
            for c in candidates:
                ch5m  = c.get("change_5m", 0)
                ch1m  = c.get("change_1m", 0)
                ch24  = c.get("change_24h", 0)
                spike = c.get("volume_spike", 1.0)
                rows.append({
                    "Symbol":    c["symbol"].replace("USDT", "/USDT"),
                    "Preis $":   f"${c['price_usd']:.6f}",
                    "5m %":      ch5m,
                    "1m %":      ch1m,
                    "24h %":     ch24,
                    "Vol Spike": spike,
                    "Vol 24h":   c["volume_24h"] / 1e6,
                })

            df = pd.DataFrame(rows)

            def _color_pct(v):
                if isinstance(v, float):
                    if v > 0:  return "color: #00e6a7; font-weight: 700"
                    if v < 0:  return "color: #ff5c5c"
                return "color: #64748b"

            def _color_spike(v):
                if isinstance(v, float):
                    if v >= 3: return "color: #00e6a7; font-weight: 700"
                    if v >= 2: return "color: #fbbf24; font-weight: 600"
                return "color: #94a3b8"

            styled = (
                df.style
                .format({"5m %": "{:+.3f}%", "1m %": "{:+.3f}%", "24h %": "{:+.2f}%",
                         "Vol Spike": "{:.1f}×", "Vol 24h": "${:.1f}M"})
                .map(_color_pct, subset=["5m %", "1m %", "24h %"])
                .map(_color_spike, subset=["Vol Spike"])
                .set_properties(**{"background-color": "#0c0f16", "color": "#e2e8f0"})
            )
            st.dataframe(styled, use_container_width=True, height=420)
        else:
            st.info("⏳ Momentum-Daten werden gesammelt (ca. 1 Minute nach Start)...")

    with right:
        st.markdown('<div class="section-header">📊 Markt-Verteilung 24h</div>', unsafe_allow_html=True)

        all_df = pd.DataFrame(list(tickers.values()))
        if not all_df.empty:
            bins   = [-100, -10, -5, -2, 0, 2, 5, 10, 100]
            labels = ["<-10%", "-10→-5%", "-5→-2%", "-2→0%", "0→2%", "2→5%", "5→10%", ">10%"]
            all_df["bucket"] = pd.cut(all_df["change_24h"], bins=bins, labels=labels)
            dist = all_df["bucket"].value_counts().reindex(labels, fill_value=0).reset_index()
            dist.columns = ["Range", "Count"]

            colors = ["#7f1d1d","#ff5c5c","#fca5a5","#94a3b8","#4ade80","#22c55e","#00e6a7","#00ffd0"]
            fig = go.Figure(go.Bar(
                x=dist["Count"], y=dist["Range"],
                orientation="h",
                marker_color=colors,
                text=dist["Count"], textposition="outside",
                textfont=dict(color="#94a3b8", size=11),
            ))
            fig.update_layout(
                plot_bgcolor="#0c0f16", paper_bgcolor="#0c0f16",
                font=dict(color="#94a3b8", family="Inter"),
                height=260, margin=dict(l=10, r=30, t=8, b=8),
                xaxis=dict(gridcolor="#151b27", zeroline=False, color="#64748b"),
                yaxis=dict(gridcolor="#151b27", color="#94a3b8"),
            )
            st.plotly_chart(fig, use_container_width=True)

            # ── Top 15 Volume bar ─────────────────────────────────────────────
            st.markdown('<div class="section-header" style="margin-top:8px">💧 Top 15 Volume</div>', unsafe_allow_html=True)
            top_vol = all_df.nlargest(15, "volume_24h")
            fig2 = go.Figure(go.Bar(
                x=top_vol["symbol"].str.replace("USDT", ""),
                y=top_vol["volume_24h"] / 1e6,
                marker=dict(
                    color=top_vol["change_24h"],
                    colorscale=[[0, "#ff5c5c"], [0.5, "#334155"], [1, "#00e6a7"]],
                    showscale=False,
                ),
                text=[f"${v:.0f}M" for v in top_vol["volume_24h"] / 1e6],
                textposition="outside",
                textfont=dict(color="#94a3b8", size=10),
            ))
            fig2.update_layout(
                plot_bgcolor="#0c0f16", paper_bgcolor="#0c0f16",
                font=dict(color="#94a3b8", family="Inter"),
                height=220, margin=dict(l=10, r=10, t=8, b=40),
                yaxis_title="Mio $", yaxis=dict(gridcolor="#151b27", color="#64748b"),
                xaxis=dict(tickangle=-45, color="#94a3b8"),
            )
            st.plotly_chart(fig2, use_container_width=True)

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
