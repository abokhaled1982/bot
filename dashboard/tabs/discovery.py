"""Discovery tab — real-time LLM ticker extraction, velocity charts, signal funnel."""
import streamlit as st
import pandas as pd
from dashboard.db import get_news_signals, get_candidates, get_velocity_data, get_signal_funnel
from dashboard.components import fmt_usd, fmt_pct, kpi_card

def render():
    st.markdown('<div class="section-header">🔍 Live Discovery Feed</div>', unsafe_allow_html=True)

    # ── Signal Funnel KPIs ────────────────────────────────────
    funnel = get_signal_funnel()
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(kpi_card("📰 Headlines", f"{funnel['Headlines Scanned']:,}", "LLM analyzed"), unsafe_allow_html=True)
    c2.markdown(kpi_card("🏷 Tickers Found", f"{funnel['Tickers Extracted']:,}", "from news"), unsafe_allow_html=True)
    c3.markdown(kpi_card("✅ Signals", f"{funnel['Signals Passed']:,}", "passed gates"), unsafe_allow_html=True)
    c4.markdown(kpi_card("💰 Executed", f"{funnel['Trades Executed']:,}", "trades"), unsafe_allow_html=True)

    st.markdown("")

    # ── News Velocity Chart ───────────────────────────────────
    @st.fragment(run_every="15s")
    def _velocity():
        st.markdown(
            '<span style="color:#7cb4ff;font-size:0.85rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">'
            '📊 News Velocity (Last 2 Hours)</span>', unsafe_allow_html=True)
        vel = get_velocity_data()
        if vel.empty:
            st.info("No velocity data yet — waiting for LLM to extract tickers from news...")
            return

        # Bar chart of mentions
        chart_data = vel.set_index("ticker")[["mentions"]].head(15)
        st.bar_chart(chart_data, height=250, color="#00e6a7")

        # Velocity table with sentiment coloring
        st.markdown("")
        for _, row in vel.iterrows():
            sent = float(row.get("avg_sent", 0))
            sent_color = "#00e6a7" if sent > 0.1 else "#ff5c5c" if sent < -0.1 else "#94a3b8"
            urg = int(row.get("max_urgency", 1))
            urg_icon = "🔥" * min(urg, 5)
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;padding:6px 12px;'
                f'background:rgba(30,41,59,0.5);border-radius:8px;margin-bottom:4px">'
                f'<span style="color:#f1f5f9;font-weight:700;font-size:0.9rem">{row["ticker"]}</span>'
                f'<span style="color:#94a3b8;font-size:0.8rem">{int(row["mentions"])} mentions</span>'
                f'<span style="color:{sent_color};font-size:0.8rem;font-weight:600">'
                f'{"+" if sent>0 else ""}{sent:.2f}</span>'
                f'<span style="font-size:0.8rem">{urg_icon}</span>'
                f'</div>', unsafe_allow_html=True)
    _velocity()

    st.markdown("---")

    # ── Candidate Evaluation History ──────────────────────────
    @st.fragment(run_every="15s")
    def _candidates():
        st.markdown(
            '<span style="color:#7cb4ff;font-size:0.85rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">'
            '🎯 Candidate Evaluations</span>', unsafe_allow_html=True)
        df = get_candidates(30)
        if df.empty:
            st.info("No candidates evaluated yet.")
            return
        st.dataframe(
            df, hide_index=True, use_container_width=True,
            column_config={
                "ticker": "Ticker", "mention_count": "Mentions",
                "velocity_score": st.column_config.NumberColumn("Velocity", format="%.3f"),
                "avg_sentiment": st.column_config.NumberColumn("Sentiment", format="%.2f"),
                "ta_score": st.column_config.NumberColumn("TA", format="%.2f"),
                "fusion_score": st.column_config.NumberColumn("Fusion", format="%.3f"),
                "llm_conviction": st.column_config.NumberColumn("Conviction", format="%.2f"),
                "decision": "Decision", "gates_passed": "Gates",
                "rejection_reason": "Reason", "cycle": "Cycle"})
    _candidates()

    st.markdown("---")

    # ── Raw Signal Feed ───────────────────────────────────────
    @st.fragment(run_every="15s")
    def _signals():
        st.markdown(
            '<span style="color:#7cb4ff;font-size:0.85rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">'
            '📡 Raw LLM Signal Feed</span>', unsafe_allow_html=True)
        df = get_news_signals(50)
        if df.empty:
            st.info("No signals extracted yet.")
            return
        st.dataframe(
            df, hide_index=True, use_container_width=True,
            column_config={
                "ticker":"Ticker",
                "sentiment":st.column_config.NumberColumn("Sent",format="%.2f"),
                "urgency":"Urg", "headline":"Headline",
                "source":"Source", "extracted_at":"Time"})
    _signals()
