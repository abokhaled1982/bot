"""
binance_dashboard.py — Live Binance Market Scanner & Strategy Visualizer

Run with:  streamlit run binance_dashboard.py
"""
import asyncio
import threading
import time
from collections import deque

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.adapters.binance_stream import BinanceStreamAdapter

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Binance Live Scanner",
    layout="wide",
    page_icon="📡",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700;800&display=swap');
  html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
  .main { background: #0b0f1a; }
  .stApp { background: #0b0f1a; color: #e2e8f0; }

  .metric-card {
    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
    border: 1px solid #1e3a5f;
    border-radius: 12px;
    padding: 18px 22px;
    margin: 4px 0;
  }
  .metric-label {
    color: #64748b;
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
  }
  .metric-value {
    color: #f1f5f9;
    font-size: 1.6rem;
    font-weight: 800;
    margin-top: 4px;
  }
  .positive { color: #00e6a7 !important; }
  .negative { color: #ff5c5c !important; }
  .neutral  { color: #94a3b8 !important; }

  .strategy-box {
    background: linear-gradient(135deg, #1a2744 0%, #0f1f3d 100%);
    border: 1px solid #2563eb44;
    border-left: 4px solid #3b82f6;
    border-radius: 10px;
    padding: 18px 22px;
    margin: 8px 0;
  }
  .strategy-title {
    color: #60a5fa;
    font-size: 0.85rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 8px;
  }
  .gate-box {
    background: #0f172a;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 10px 14px;
    margin: 4px 0;
    font-size: 0.82rem;
  }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 700;
  }
  .badge-green  { background: #00e6a715; color: #00e6a7; border: 1px solid #00e6a730; }
  .badge-blue   { background: #3b82f615; color: #60a5fa; border: 1px solid #3b82f630; }
  .badge-yellow { background: #f59e0b15; color: #fbbf24; border: 1px solid #f59e0b30; }
  .badge-red    { background: #ff5c5c15; color: #ff5c5c; border: 1px solid #ff5c5c30; }

  div[data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; }
  .stButton button {
    background: linear-gradient(135deg, #3b82f6, #2563eb);
    color: white; border: none; border-radius: 8px;
    font-weight: 700; padding: 8px 18px;
  }
  h1, h2, h3 { color: #f1f5f9; }
</style>
""", unsafe_allow_html=True)

# ── Global adapter singleton (shared across reruns) ───────────────────────────

@st.cache_resource
def get_adapter():
    adapter = BinanceStreamAdapter()

    def run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(adapter.start())

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()
    return adapter

adapter = get_adapter()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        '<div style="text-align:center;padding:12px 0 6px">'
        '<span style="font-size:1.3rem;font-weight:800;color:#60a5fa">📡 BINANCE</span><br>'
        '<span style="font-size:0.75rem;color:#475569;letter-spacing:2px">LIVE SCANNER</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")

    status = adapter.status()
    dot_color = "#00e6a7" if status["connected"] else "#ff5c5c"
    dot_label = "Connected" if status["connected"] else "Connecting..."
    st.markdown(
        f'<span style="color:{dot_color};font-size:1rem">●</span> '
        f'<span style="color:#94a3b8;font-size:0.85rem">{dot_label}</span>',
        unsafe_allow_html=True,
    )

    st.markdown(f"""
    <div style="margin-top:12px;font-size:0.8rem;color:#64748b;line-height:2">
    📊 Symbols tracked: <span style="color:#f1f5f9;font-weight:700">{status['tracked_symbols']}</span><br>
    🎯 Candidates: <span style="color:#00e6a7;font-weight:700">{status['candidates']}</span><br>
    ⏱ Last update: <span style="color:#f1f5f9">{status['age_sec']}s ago</span>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")
    auto_refresh = st.toggle("Auto-Refresh (5s)", value=True)
    top_n = st.slider("Show top N coins", 5, 30, 15)

    st.markdown("---")
    st.markdown("""
    <div style="font-size:0.72rem;color:#475569;line-height:1.8">
    <b style="color:#64748b">DATA SOURCE</b><br>
    Binance WebSocket<br>
    <code>!miniTicker@arr</code><br>
    ~1 second update interval
    </div>
    """, unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("# 📡 Binance Live Market Scanner")
st.markdown(
    f'<span style="color:#64748b;font-size:0.85rem">Live data · {time.strftime("%H:%M:%S")} · '
    f'{status["tracked_symbols"]} USDT pairs tracked</span>',
    unsafe_allow_html=True,
)
st.markdown("---")

# ── Strategy Explanation ──────────────────────────────────────────────────────
with st.expander("📋 **Strategie-Erklärung — Was macht der Scanner?**", expanded=True):
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("""
        <div class="strategy-box">
          <div class="strategy-title">🎯 Ziel der Strategie</div>
          <div style="color:#cbd5e1;font-size:0.85rem;line-height:1.8">
            Der Scanner sucht nach Coins mit <b style="color:#00e6a7">überdurchschnittlichem Momentum</b>
            — also Paare, die im Vergleich zu ihrem eigenen historischen Volumen gerade
            ungewöhnlich viel gehandelt werden UND gleichzeitig im Preis steigen.
            <br><br>
            Das Prinzip: <b style="color:#60a5fa">Volumen-Spike + positive Preis-Bewegung = möglicher Breakout</b>
          </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("""
        <div class="strategy-box">
          <div class="strategy-title">🔍 Gate 1 — Minimum Liquidity</div>
          <div class="gate-box" style="color:#94a3b8">
            ✅ Mindest-Volumen: <b style="color:#f1f5f9">$1,000,000 / 24h</b><br>
            ✅ Mindest-Preis: <b style="color:#f1f5f9">$0.000001</b><br>
            ❌ Ausgeschlossen: Stablecoins, Leveraged Tokens, Wrapped Tokens
          </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("""
        <div class="strategy-box">
          <div class="strategy-title">📈 Gate 2 — Volume Spike</div>
          <div class="gate-box" style="color:#94a3b8">
            Vergleicht aktuelles Volumen mit dem <b style="color:#f1f5f9">rolling average der letzten 60 Updates</b><br><br>
            ✅ Spike-Faktor: <b style="color:#00e6a7">≥ 2.0×</b> des Durchschnitts<br>
            <span style="color:#64748b;font-size:0.78rem">→ Coin wird 2× mehr als üblich gehandelt</span>
          </div>
        </div>
        """, unsafe_allow_html=True)

    with col2:
        st.markdown("""
        <div class="strategy-box">
          <div class="strategy-title">🚀 Gate 3 — Momentum Filter</div>
          <div class="gate-box" style="color:#94a3b8">
            ✅ 5m-Preisänderung: <b style="color:#00e6a7">≥ +0.5%</b><br>
            <span style="color:#64748b;font-size:0.78rem">→ Preis muss in letzten 5 Minuten steigen</span><br><br>
            🔄 Startup-Fallback (erste 5 Min):<br>
            <span style="color:#fbbf24">24h-Change ÷ 4.8 als Proxy für 5m-Bewegung</span>
          </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("""
        <div class="strategy-box">
          <div class="strategy-title">🧮 Scoring-Formel</div>
          <div class="gate-box" style="color:#94a3b8;font-family:monospace">
            Score = <b style="color:#60a5fa">Momentum×2.0</b> + <b style="color:#a78bfa">1m-Change×1.0</b> + <b style="color:#00e6a7">min(Spike,5)×5.0</b><br><br>
            <span style="color:#64748b">Höchstes Score = bester Kandidat</span>
          </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("""
        <div class="strategy-box">
          <div class="strategy-title">⚡ Nächste Schritte (noch nicht aktiv)</div>
          <div class="gate-box" style="color:#94a3b8">
            🔲 RSI-Bestätigung (<70 = nicht überkauft)<br>
            🔲 Binance Order Execution (Market/Limit)<br>
            🔲 Stop-Loss / Take-Profit Logik<br>
            🔲 Position-Tracking in Dashboard
          </div>
        </div>
        """, unsafe_allow_html=True)

st.markdown("---")

# ── Live Data ─────────────────────────────────────────────────────────────────
tickers = adapter.all_tickers()
candidates = adapter.get_candidates(limit=top_n)

if not tickers:
    st.info("⏳ Verbinde mit Binance WebSocket... Bitte warte 2-3 Sekunden.")
    if auto_refresh:
        time.sleep(2)
        st.rerun()
else:
    # ── Summary metrics ───────────────────────────────────────────────────────
    m1, m2, m3, m4 = st.columns(4)

    all_changes = [t["change_24h"] for t in tickers.values()]
    gainers = sum(1 for c in all_changes if c > 0)
    losers  = sum(1 for c in all_changes if c < 0)
    avg_change = sum(all_changes) / len(all_changes) if all_changes else 0
    top_gainer = max(tickers.values(), key=lambda x: x["change_24h"], default={})

    with m1:
        st.markdown(f"""
        <div class="metric-card">
          <div class="metric-label">📊 Märkte gesamt</div>
          <div class="metric-value">{len(tickers)}</div>
          <div style="color:#64748b;font-size:0.75rem">USDT Spot Paare</div>
        </div>""", unsafe_allow_html=True)

    with m2:
        color = "positive" if avg_change > 0 else "negative"
        with m2:
            st.markdown(f"""
            <div class="metric-card">
              <div class="metric-label">📈 Markt-Stimmung</div>
              <div class="metric-value {color}">{avg_change:+.2f}%</div>
              <div style="color:#64748b;font-size:0.75rem">Ø 24h Änderung</div>
            </div>""", unsafe_allow_html=True)

    with m3:
        st.markdown(f"""
        <div class="metric-card">
          <div class="metric-label">🟢 Gewinner / 🔴 Verlierer</div>
          <div class="metric-value"><span class="positive">{gainers}</span> / <span class="negative">{losers}</span></div>
          <div style="color:#64748b;font-size:0.75rem">24h-Basis</div>
        </div>""", unsafe_allow_html=True)

    with m4:
        tg_sym = top_gainer.get("symbol", "-")
        tg_ch  = top_gainer.get("change_24h", 0)
        st.markdown(f"""
        <div class="metric-card">
          <div class="metric-label">🏆 Top Gainer</div>
          <div class="metric-value positive">{tg_sym.replace('USDT','')}</div>
          <div style="color:#00e6a7;font-size:0.85rem;font-weight:700">{tg_ch:+.2f}% (24h)</div>
        </div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Two-column layout ─────────────────────────────────────────────────────
    left, right = st.columns([2, 1])

    with left:
        st.markdown("### 🎯 Top Scanner-Kandidaten")
        st.markdown(
            '<span style="color:#64748b;font-size:0.8rem">Coins mit höchstem Momentum-Score jetzt</span>',
            unsafe_allow_html=True,
        )

        if candidates:
            rows = []
            for c in candidates:
                ch5m = c.get("change_5m", 0)
                ch1m = c.get("change_1m", 0)
                ch24 = c.get("change_24h", 0)
                spike = c.get("volume_spike", 1.0)
                rows.append({
                    "Symbol":    c["symbol"].replace("USDT", "/USDT"),
                    "Preis $":   f"{c['price_usd']:.6f}",
                    "5m %":      f"{ch5m:+.3f}%",
                    "1m %":      f"{ch1m:+.3f}%",
                    "24h %":     f"{ch24:+.2f}%",
                    "Vol Spike": f"{spike:.1f}×",
                    "Vol 24h":   f"${c['volume_24h']/1e6:.1f}M",
                })
            df = pd.DataFrame(rows)

            def color_pct(val):
                try:
                    v = float(val.replace("%","").replace("+",""))
                    if v > 0: return "color: #00e6a7; font-weight: 700"
                    if v < 0: return "color: #ff5c5c"
                    return "color: #94a3b8"
                except: return ""

            def color_spike(val):
                try:
                    v = float(val.replace("×",""))
                    if v >= 3: return "color: #00e6a7; font-weight: 700"
                    if v >= 2: return "color: #fbbf24"
                    return "color: #94a3b8"
                except: return ""

            styled = df.style.map(color_pct, subset=["5m %", "1m %", "24h %"]) \
                             .map(color_spike, subset=["Vol Spike"]) \
                             .set_properties(**{"background-color": "#0f172a", "color": "#e2e8f0"})

            st.dataframe(styled, use_container_width=True, height=400)
        else:
            st.info("⏳ Warte auf Momentum-Daten (ca. 1 Minute nach Start)...")

    with right:
        st.markdown("### 📊 Markt-Verteilung")

        all_df = pd.DataFrame(list(tickers.values()))
        if not all_df.empty:
            bins = [-100, -5, -2, -0.5, 0.5, 2, 5, 100]
            labels = ["<-5%", "-5→-2%", "-2→-0.5%", "-0.5→0.5%", "0.5→2%", "2→5%", ">5%"]
            all_df["bucket"] = pd.cut(all_df["change_24h"], bins=bins, labels=labels)
            dist = all_df["bucket"].value_counts().reindex(labels, fill_value=0).reset_index()
            dist.columns = ["Bereich", "Anzahl"]

            colors = ["#ff2b2b","#ff5c5c","#ff8c8c","#64748b","#4ade80","#22c55e","#00e6a7"]
            fig = go.Figure(go.Bar(
                x=dist["Anzahl"], y=dist["Bereich"],
                orientation="h",
                marker_color=colors,
                text=dist["Anzahl"],
                textposition="outside",
            ))
            fig.update_layout(
                plot_bgcolor="#0f172a", paper_bgcolor="#0f172a",
                font=dict(color="#94a3b8", family="Inter"),
                height=280,
                margin=dict(l=10, r=30, t=10, b=10),
                xaxis=dict(gridcolor="#1e293b", zeroline=False),
                yaxis=dict(gridcolor="#1e293b"),
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)

            # Volume bubble chart (top 15 by volume)
            st.markdown("### 💧 Volume Top 15")
            top_vol = all_df.nlargest(15, "volume_24h")
            fig2 = go.Figure(go.Bar(
                x=top_vol["symbol"].str.replace("USDT",""),
                y=top_vol["volume_24h"] / 1e6,
                marker=dict(
                    color=top_vol["change_24h"],
                    colorscale=[[0,"#ff5c5c"],[0.5,"#334155"],[1,"#00e6a7"]],
                    showscale=True,
                    colorbar=dict(title="24h %", tickfont=dict(color="#94a3b8")),
                ),
                text=[f"${v:.0f}M" for v in top_vol["volume_24h"] / 1e6],
                textposition="outside",
            ))
            fig2.update_layout(
                plot_bgcolor="#0f172a", paper_bgcolor="#0f172a",
                font=dict(color="#94a3b8", family="Inter"),
                height=260,
                margin=dict(l=10, r=10, t=10, b=30),
                yaxis_title="Vol (Mio $)",
                xaxis=dict(tickangle=-45),
                showlegend=False,
            )
            st.plotly_chart(fig2, use_container_width=True)

    # ── Full market table ─────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 📋 Alle Märkte")
    search = st.text_input("🔍 Suche (z.B. BTC, ETH, SOL)", "")

    all_rows = []
    for sym, td in sorted(tickers.items()):
        if search and search.upper() not in sym:
            continue
        all_rows.append({
            "Symbol":   sym.replace("USDT", "/USDT"),
            "Preis $":  f"{td['price_usd']:.6f}",
            "24h %":    f"{td['change_24h']:+.2f}%",
            "Hoch 24h": f"{td['high_24h']:.6f}",
            "Tief 24h": f"{td['low_24h']:.6f}",
            "Vol 24h":  f"${td['volume_24h']/1e6:.1f}M",
        })

    if all_rows:
        full_df = pd.DataFrame(all_rows)
        styled_full = full_df.style.map(color_pct, subset=["24h %"]) \
                                   .set_properties(**{"background-color": "#0f172a", "color": "#e2e8f0"})
        st.dataframe(styled_full, use_container_width=True, height=350)

# ── Auto-refresh ──────────────────────────────────────────────────────────────
if auto_refresh:
    time.sleep(5)
    st.rerun()
