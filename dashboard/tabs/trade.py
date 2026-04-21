import json
import streamlit as st
from dashboard.db import db_query, get_live_price
from dashboard.components import fmt_usd, fmt_pct
from dashboard.config import WALLET_ADDRESS


def render():
    st.markdown('<div class="section-header">🔄 Manual Trade</div>', unsafe_allow_html=True)

    col_buy, col_sell = st.columns(2)

    with col_buy:
        st.markdown(
            '<div style="background:#0c0f16;border:1px solid #151b27;border-radius:12px;padding:20px">'
            '<span style="color:#00e6a7;font-weight:700;font-size:0.9rem">🟢 Manual BUY</span></div>',
            unsafe_allow_html=True,
        )
        buy_addr   = st.text_input("Token Address", key="manual_buy_addr", placeholder="Solana mint address...")
        buy_amount = st.number_input("Amount (USD)", min_value=0.01, value=0.20, step=0.05, key="manual_buy_amt")
        if st.button("BUY", type="primary", key="manual_buy_btn", use_container_width=True):
            if buy_addr.strip():
                trigger = {"action": "BUY", "address": buy_addr.strip(), "amount": buy_amount}
                with open("MANUAL_TRADE", "w") as f:
                    json.dump(trigger, f)
                st.success(f"BUY ${buy_amount:.2f} queued for `{buy_addr[:20]}...`")
                st.info("Bot will execute on the next scan cycle (~30s).")
            else:
                st.error("Please enter a token address.")

    with col_sell:
        st.markdown(
            '<div style="background:#0c0f16;border:1px solid #151b27;border-radius:12px;padding:20px">'
            '<span style="color:#ff5c5c;font-weight:700;font-size:0.9rem">🔴 Manual SELL</span></div>',
            unsafe_allow_html=True,
        )
        sell_addr = st.text_input("Token Address", key="manual_sell_addr", placeholder="Solana mint address...")
        sell_pct  = st.selectbox("Sell %", [25, 50, 75, 100], index=3, key="manual_sell_pct")
        if st.button("SELL", type="primary", key="manual_sell_btn", use_container_width=True):
            if sell_addr.strip():
                trigger = {"action": "SELL", "address": sell_addr.strip(), "sell_pct": sell_pct / 100}
                with open("MANUAL_TRADE", "w") as f:
                    json.dump(trigger, f)
                st.success(f"SELL {sell_pct}% queued for `{sell_addr[:20]}...`")
                st.info("Bot will execute on the next scan cycle (~30s).")
            else:
                st.error("Please enter a token address.")

    st.markdown("---")

    # ── Recent manual trades ─────────────────────────────────────────────────
    st.markdown(
        '<span style="color:#7cb4ff;font-size:0.85rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">'
        '📋 Recent Manual Trades</span>',
        unsafe_allow_html=True,
    )
    df = db_query("""
        SELECT symbol, token_address, decision, entry_price, buy_amount_usd, sell_amount_usd,
               tx_status, tx_signature, timestamp
        FROM trades
        WHERE funnel_stage = 'MANUAL'
        ORDER BY timestamp DESC LIMIT 20
    """)
    if df.empty:
        st.info("No manual trades yet.")
    else:
        for _, row in df.iterrows():
            dec  = str(row["decision"])
            ep   = float(row["entry_price"] or 0)
            b_u  = float(row.get("buy_amount_usd",  0) or 0)
            s_u  = float(row.get("sell_amount_usd", 0) or 0)
            tx_s = str(row.get("tx_status") or "")
            icon = "🟢" if "BUY" in dec else "🔴"
            ts   = str(row["timestamp"] or "")[:16]
            sym  = str(row["symbol"] or "?")

            amt_str = f"for {fmt_usd(b_u)}" if b_u > 0 else (f"→ got {fmt_usd(s_u)}" if s_u > 0 else "")
            tx_badge = {"confirmed": "✅", "unconfirmed": "⚠️", "error": "❌"}.get(tx_s, "")

            st.markdown(
                f'<div class="event-row">'
                f'<span class="event-icon">{icon}</span>'
                f'<span class="event-time">{ts}</span>'
                f'<span class="event-sym">{sym}</span>'
                f'<span style="color:#ffffff;font-size:0.88rem">{dec} @ ${ep:.8f} {amt_str} {tx_badge}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            if row.get("tx_signature"):
                st.markdown(
                    f'<a href="https://solscan.io/tx/{row["tx_signature"]}" target="_blank" '
                    f'style="color:#3b8bff;font-size:0.85rem;margin-left:46px;text-decoration:none">🔗 View TX</a>',
                    unsafe_allow_html=True,
                )
