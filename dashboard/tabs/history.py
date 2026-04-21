"""
dashboard/tabs/history.py — Binance Trade History Tab
Reads from SQLite (trades + bot_events tables) written by binance_pipeline.py
"""
import os
import json
import requests
import streamlit as st
import pandas as pd
from datetime import datetime

from dashboard.db import db_query, get_recent_events

POSITION_SIZE_USD = float(os.getenv("BINANCE_POSITION_SIZE_USDT", "10.0"))


@st.cache_data(ttl=15)
def _get_binance_price(symbol: str) -> float:
    """Fetch live Binance price for a symbol like BTCUSDT."""
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
            timeout=4,
        )
        if r.status_code == 200:
            return float(r.json().get("price", 0))
    except Exception:
        pass
    return 0.0


def _load_positions() -> dict:
    if os.path.exists("positions.json"):
        try:
            with open("positions.json") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _fmt_usd(v: float) -> str:
    if abs(v) >= 1000:
        return f"${v:,.2f}"
    return f"${v:.4f}"


def _pct_html(pct: float) -> str:
    color = "#00e6a7" if pct >= 0 else "#ff5c5c"
    sign  = "+" if pct >= 0 else ""
    return f'<span style="color:{color};font-weight:700">{sign}{pct:.2f}%</span>'


def _kpi(label: str, value: str, sub: str = "") -> str:
    return (
        f'<div class="kpi-card">'
        f'<div class="label">{label}</div>'
        f'<div class="value">{value}</div>'
        f'{"<div class=sub>" + sub + "</div>" if sub else ""}'
        f'</div>'
    )


def render():
    st.markdown('<div class="section-header">📋 Binance Trade History</div>', unsafe_allow_html=True)

    # ── Load all trades from DB ────────────────────────────────────────────────
    df = db_query(
        """SELECT id, symbol, token_address, entry_price, position_size, score,
                  decision, buy_amount_usd, sell_amount_usd,
                  funnel_stage, gates_passed, timestamp
           FROM trades
           ORDER BY timestamp DESC
           LIMIT 500"""
    )

    # ── Summary KPIs ──────────────────────────────────────────────────────────
    buy_df  = df[df["decision"].str.contains("BUY",  na=False)] if not df.empty else pd.DataFrame()
    sell_df = df[df["decision"].str.contains("SELL", na=False)] if not df.empty else pd.DataFrame()

    total_trades  = len(buy_df)
    total_invested = buy_df["buy_amount_usd"].fillna(POSITION_SIZE_USD).sum() if not buy_df.empty else 0
    avg_score     = buy_df["score"].mean() if not buy_df.empty else 0

    k1, k2, k3, k4 = st.columns(4)
    k1.markdown(_kpi("📊 Total Buys", str(total_trades), "paper trades"), unsafe_allow_html=True)
    k2.markdown(_kpi("💵 Total Deployed", _fmt_usd(total_invested), "USDT simulated"), unsafe_allow_html=True)
    k3.markdown(_kpi("⭐ Avg Score", f"{avg_score:.1f}", "fusion score"), unsafe_allow_html=True)
    k4.markdown(_kpi("🔢 Open Positions", str(len(_load_positions())), "in portfolio"), unsafe_allow_html=True)

    st.markdown("")

    if df.empty:
        st.info("📭 No trades yet. Start `python3 main.py` to begin scanning.")
        return

    # ── Filters ───────────────────────────────────────────────────────────────
    f1, f2, f3 = st.columns([2, 1.5, 1.5])
    with f1:
        search = st.text_input("🔍 Symbol suchen", "", placeholder="z.B. BTC, ETH, SOL", key="hist_search")
    with f2:
        dec_filter = st.selectbox("Decision", ["All", "BUY", "SELL", "SKIP"], key="hist_dec")
    with f3:
        period = st.selectbox("Zeitraum", ["Alle", "Heute", "7 Tage", "30 Tage"], key="hist_period")

    # Apply filters
    filtered = df.copy()
    if search:
        filtered = filtered[filtered["symbol"].str.contains(search.upper(), na=False)]
    if dec_filter != "All":
        filtered = filtered[filtered["decision"].str.contains(dec_filter, na=False)]
    if period == "Heute":
        filtered = filtered[pd.to_datetime(filtered["timestamp"]).dt.date == datetime.now().date()]
    elif period == "7 Tage":
        filtered = filtered[pd.to_datetime(filtered["timestamp"]) >= pd.Timestamp.now() - pd.Timedelta(days=7)]
    elif period == "30 Tage":
        filtered = filtered[pd.to_datetime(filtered["timestamp"]) >= pd.Timestamp.now() - pd.Timedelta(days=30)]

    st.caption(f"{len(filtered)} Einträge")

    # ── Open Positions (live P/L) ─────────────────────────────────────────────
    positions = _load_positions()
    if positions:
        st.markdown("---")
        st.markdown('<div class="section-header">📌 Offene Positionen (Paper)</div>', unsafe_allow_html=True)

        rows = []
        for sym, pos in positions.items():
            ep     = float(pos.get("entry_price", 0))
            size   = float(pos.get("size_usdt", POSITION_SIZE_USD))
            opened = pos.get("opened_at", 0)
            age_m  = int((datetime.now().timestamp() - opened) / 60) if opened else 0

            live_price = _get_binance_price(sym)
            pct = ((live_price - ep) / ep * 100) if ep > 0 and live_price > 0 else 0
            pnl = size * pct / 100

            rows.append({
                "Symbol":    sym.replace("USDT", "/USDT"),
                "Entry $":   ep,
                "Live $":    live_price,
                "P/L %":     pct,
                "P/L $":     pnl,
                "Size":      size,
                "Alter":     f"{age_m}m",
            })

        pos_df = pd.DataFrame(rows)

        def _color_pnl(v):
            if isinstance(v, float):
                return "color:#00e6a7;font-weight:700" if v > 0 else "color:#ff5c5c;font-weight:700"
            return ""

        styled = (
            pos_df.style
            .format({
                "Entry $":  "${:.6f}",
                "Live $":   "${:.6f}",
                "P/L %":    "{:+.2f}%",
                "P/L $":    "${:+.4f}",
                "Size":     "${:.2f}",
            })
            .map(_color_pnl, subset=["P/L %", "P/L $"])
            .set_properties(**{"background-color": "#0c0f16", "color": "#e2e8f0"})
        )
        st.dataframe(styled, use_container_width=True, height=min(200, len(rows) * 40 + 60))

    # ── Trade Table ───────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown('<div class="section-header">📋 Alle Trades</div>', unsafe_allow_html=True)

    for _, row in filtered.iterrows():
        sym   = str(row.get("symbol", "?"))
        dec   = str(row.get("decision", ""))
        ep    = float(row.get("entry_price", 0) or 0)
        score = float(row.get("score", 0) or 0)
        size  = float(row.get("buy_amount_usd", POSITION_SIZE_USD) or POSITION_SIZE_USD)
        gates = str(row.get("gates_passed", "") or "")
        ts    = str(row.get("timestamp", ""))[:16]
        stage = str(row.get("funnel_stage", "") or "")

        # Live P/L for open positions
        live_price = 0.0
        pct = 0.0
        is_open = sym in positions
        if is_open:
            live_price = _get_binance_price(sym)
            pct = ((live_price - ep) / ep * 100) if ep > 0 and live_price > 0 else 0

        # Expander label
        if "BUY" in dec:
            pct_tag = f" · {_pct_html(pct)} live" if is_open else ""
            icon = "🟢" if is_open else "⬛"
            label = f"{icon} **{sym.replace('USDT','/USDT')}** · BUY @ ${ep:.6f} · Score:{score:.0f} · {ts}{pct_tag}"
        elif "SELL" in dec:
            label = f"🔴 **{sym.replace('USDT','/USDT')}** · SELL @ ${ep:.6f} · {ts}"
        else:
            label = f"🔵 **{sym.replace('USDT','/USDT')}** · {dec} · Score:{score:.0f} · {ts}"

        with st.expander(label, expanded=False):
            col1, col2 = st.columns(2)

            with col1:
                # P/L hero for open positions
                if is_open and live_price > 0:
                    pnl_usd = size * pct / 100
                    st.markdown(
                        f'<div style="margin-bottom:12px">'
                        f'{_pct_html(pct)}'
                        f'<span style="color:#64748b;margin-left:8px;font-size:0.9rem">'
                        f'({_fmt_usd(pnl_usd)})</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                st.markdown(
                    f'<div style="font-size:0.82rem;line-height:2;color:#94a3b8">'
                    f'<b style="color:#e2e8f0">Symbol:</b> {sym.replace("USDT", "/USDT")}<br>'
                    f'<b style="color:#e2e8f0">Decision:</b> {dec}<br>'
                    f'<b style="color:#e2e8f0">Entry Price:</b> ${ep:.8f}<br>'
                    f'<b style="color:#e2e8f0">Size:</b> {_fmt_usd(size)} USDT<br>'
                    f'<b style="color:#e2e8f0">Score:</b> {score:.1f}<br>'
                    f'<b style="color:#e2e8f0">Stage:</b> {stage}<br>'
                    f'<b style="color:#e2e8f0">Time:</b> {ts}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            with col2:
                if live_price > 0:
                    st.markdown(
                        f'<div style="font-size:0.82rem;line-height:2;color:#94a3b8">'
                        f'<b style="color:#e2e8f0">Live Price:</b> ${live_price:.8f}<br>'
                        f'<b style="color:#e2e8f0">Gates:</b> {gates}<br>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f'<div style="font-size:0.82rem;line-height:2;color:#94a3b8">'
                        f'<b style="color:#e2e8f0">Gates:</b> {gates}<br>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                # Binance link
                binance_sym = sym.replace("USDT", "")
                st.markdown(
                    f'<a href="https://www.binance.com/en/trade/{binance_sym}_USDT" target="_blank" '
                    f'style="color:#3b8bff;font-size:0.85rem;text-decoration:none">📊 Binance Chart</a>',
                    unsafe_allow_html=True,
                )
