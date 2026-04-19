import time
import json
import streamlit as st
from dashboard.config import (
    POSITION_SIZE_USD, STOP_LOSS_PCT,
    TRAILING_STOP_PCT, TRAILING_ACTIVATE, TP1_PCT, TP2_PCT, TP3_PCT, MAX_HOLD_HOURS,
)
from dashboard.db import db_query, get_live_price, get_token_full_info, get_reconciled_positions
from dashboard.components import fmt_usd, fmt_pct, kpi_card

def render():
    st.markdown('<div class="section-header">🟢 Open Positions</div>', unsafe_allow_html=True)

    # ── Summary KPIs ─────────────────────────────────────────────────────────
    df_stats = db_query("""
        SELECT
            SUM(CASE WHEN action LIKE '%BUY%'  THEN quantity * price ELSE 0 END) as total_invested,
            SUM(CASE WHEN action LIKE '%SELL%' THEN quantity * price ELSE 0 END) as total_returned,
            COUNT(DISTINCT CASE WHEN action LIKE '%BUY%' THEN ticker END) as tokens_bought
        FROM trades
    """)
    if not df_stats.empty:
        ps  = df_stats.iloc[0]
        inv = float(ps.get("total_invested") or 0)
        ret = float(ps.get("total_returned") or 0)
        net = ret - inv
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(kpi_card("Tickers Bought", str(int(ps.get("tokens_bought") or 0)), "unique stocks"), unsafe_allow_html=True)
        c2.markdown(kpi_card("Total Invested", fmt_usd(inv), "all BUYs"), unsafe_allow_html=True)
        c3.markdown(kpi_card("Total Returned", fmt_usd(ret), "all SELLs"), unsafe_allow_html=True)
        net_html = f'<span class="{"profit" if net >= 0 else "loss"}">{fmt_usd(net)}</span>'
        c4.markdown(kpi_card("Net P/L", net_html, "realized"), unsafe_allow_html=True)

    st.markdown("")

    # ── Live positions (auto-refresh) ─────────────────────────────────────────
    @st.fragment(run_every="30s")
    def _render_open():
        positions = get_reconciled_positions()
        if not positions:
            st.info("No open positions.")
            return

        total_invested = sum(pos.get("quantity", 0) * pos.get("entry_price", 0) for pos in positions.values())
        total_current  = sum(pos.get("quantity", 0) * pos.get("current_price", 0) for pos in positions.values())
        total_pl = sum(pos.get("pnl", 0) for pos in positions.values())
        pl_pct   = (total_pl / total_invested * 100) if total_invested > 0 else 0
        
        pc1, pc2, pc3, pc4 = st.columns(4)
        pc1.markdown(kpi_card("Active", str(len(positions)), "positions"), unsafe_allow_html=True)
        pc2.markdown(kpi_card("Invested", fmt_usd(total_invested), "live"), unsafe_allow_html=True)
        pc3.markdown(kpi_card("Current Value", fmt_usd(total_current), "live"), unsafe_allow_html=True)
        pl_html = f'<span class="{"profit" if total_pl >= 0 else "loss"}">{fmt_usd(total_pl)}</span>'
        pc4.markdown(kpi_card("Unrealized P/L", pl_html, fmt_pct(pl_pct)), unsafe_allow_html=True)

        st.markdown("")

        for ticker, pos in positions.items():
            ep      = pos.get("entry_price", 0)
            sym     = pos.get("symbol", ticker)
            cp      = pos.get("current_price", 0)
            pl_usd  = pos.get("pnl", 0)
            qty     = pos.get("quantity", 0)
            
            invested = qty * ep
            cur_val  = qty * cp
            pl_pct   = ((cp - ep) / ep * 100) if ep > 0 else 0
            
            sg       = "+" if pl_pct >= 0 else ""
            clr      = "profit" if pl_pct >= 0 else "loss"

            exp_label = f"{'🔥' if pl_pct > 20 else '🟢'} **{sym}** · {sg}{pl_pct:.1f}% · {fmt_usd(pl_usd)}"

            with st.expander(exp_label, expanded=len(positions) <= 4):
                st.markdown(
                    f'<div style="display:flex;align-items:baseline;gap:16px;margin-bottom:12px">'
                    f'<span style="font-size:1.6rem;font-weight:800;color:#f1f5f9">{sym}</span>'
                    f'<span class="{clr}" style="font-size:1.4rem;font-weight:800">{sg}{pl_pct:.1f}%</span>'
                    f'<span class="{clr}" style="font-size:1rem">{sg}{fmt_usd(abs(pl_usd))}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                st.markdown(
                    f'<div class="detail-grid">'
                    f'<div class="detail-item"><div class="detail-label">💲 Entry</div><div class="detail-value">${ep:.2f}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">💲 Current</div><div class="detail-value">${cp:.2f}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">💰 Invested</div><div class="detail-value">{fmt_usd(invested)}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">💎 Current Val</div><div class="detail-value">{fmt_usd(cur_val)}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">📊 Quantity</div><div class="detail-value">{qty}</div></div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # Action button
                sell_pct = st.selectbox("Sell %", [25, 50, 75, 100], index=3, key=f"sell_{ticker}")
                if st.button(f"SELL {sell_pct}%", key=f"sellbtn_{ticker}", type="primary", use_container_width=True):
                    with open("MANUAL_TRADE", "w") as f:
                        json.dump({"action": "SELL", "ticker": ticker, "sell_pct": sell_pct / 100}, f)
                    st.success(f"Sell {sell_pct}% queued for {sym}")

    _render_open()
