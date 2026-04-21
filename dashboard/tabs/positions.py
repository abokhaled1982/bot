import time
import json
import streamlit as st
from dashboard.config import (
    WALLET_ADDRESS, POSITION_SIZE_USD, STOP_LOSS_PCT,
    TRAILING_STOP_PCT, TRAILING_ACTIVATE, TP1_PCT, TP2_PCT, TP3_PCT, MAX_HOLD_HOURS,
)
from dashboard.db import db_query, get_live_price, get_token_full_info, get_reconciled_positions
from dashboard.components import fmt_usd, fmt_pct, kpi_card


def render():
    st.markdown('<div class="section-header">🟢 Open Positions</div>', unsafe_allow_html=True)

    # ── Summary KPIs ─────────────────────────────────────────────────────────
    df_stats = db_query("""
        SELECT
            SUM(CASE WHEN decision LIKE '%BUY%'  THEN buy_amount_usd  ELSE 0 END) as total_invested,
            SUM(CASE WHEN decision LIKE '%SELL%' THEN sell_amount_usd ELSE 0 END) as total_returned,
            COUNT(DISTINCT CASE WHEN decision LIKE '%BUY%' THEN token_address END) as tokens_bought,
            COUNT(CASE WHEN decision LIKE '%SELL_FAILED%' THEN 1 END)              as sell_failed
        FROM trades
    """)
    if not df_stats.empty:
        ps  = df_stats.iloc[0]
        inv = float(ps.get("total_invested") or 0)
        ret = float(ps.get("total_returned") or 0)
        net = ret - inv
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(kpi_card("Tokens Bought", str(int(ps.get("tokens_bought") or 0)), "unique coins"), unsafe_allow_html=True)
        c2.markdown(kpi_card("Total Invested", fmt_usd(inv), "all BUYs"), unsafe_allow_html=True)
        c3.markdown(kpi_card("Total Returned", fmt_usd(ret), "all SELLs"), unsafe_allow_html=True)
        net_html = f'<span class="{"profit" if net >= 0 else "loss"}">{fmt_usd(net)}</span>'
        c4.markdown(kpi_card("Net P/L", net_html, "realized"), unsafe_allow_html=True)

    st.markdown("")

    @st.fragment(run_every="10s")
    def _render_open():
        positions = get_reconciled_positions(WALLET_ADDRESS)
        if not positions:
            st.info("Keine offenen Binance Positionen im Paper Trading.")
            return

        # Portfolio summary
        total_invested = 0.0
        total_current  = 0.0
        for addr, pos in positions.items():
            ep  = float(pos.get("entry_price", 0))
            rem = float(pos.get("remaining_pct", 1.0))
            cp  = get_live_price(addr)
            inv = float(pos.get("size_usdt", POSITION_SIZE_USD)) * rem
            cur = (inv / ep * cp) if ep > 0 and cp > 0 else inv
            total_invested += inv
            total_current  += cur

        total_pl = total_current - total_invested
        pl_pct   = (total_pl / total_invested * 100) if total_invested > 0 else 0
        pc1, pc2, pc3, pc4 = st.columns(4)
        pc1.markdown(kpi_card("Active", str(len(positions)), "positions"), unsafe_allow_html=True)
        pc2.markdown(kpi_card("Invested", fmt_usd(total_invested), "live"), unsafe_allow_html=True)
        pc3.markdown(kpi_card("Current Value", fmt_usd(total_current), "live"), unsafe_allow_html=True)
        pl_html = f'<span class="{"profit" if total_pl >= 0 else "loss"}">{fmt_usd(total_pl)}</span>'
        pc4.markdown(kpi_card("Unrealized P/L", pl_html, fmt_pct(pl_pct)), unsafe_allow_html=True)

        st.markdown("")

        for addr, pos in positions.items():
            ep      = float(pos.get("entry_price", 0))
            sym     = pos.get("symbol", addr[:8])
            rem     = float(pos.get("remaining_pct", 1.0))
            cp      = get_live_price(addr)
            ath     = float(pos.get("highest_price", ep))
            trail   = pos.get("trailing_active", False)
            created = pos.get("opened_at", pos.get("created_at", 0))
            age_h   = (time.time() - created) / 3600 if created else 0
            buy_ts  = __import__('datetime').datetime.fromtimestamp(created).strftime("%d.%m %H:%M") if created else "—"

            pl_pct   = ((cp - ep) / ep * 100)       if ep > 0 and cp > 0 else 0
            invested = float(pos.get("size_usdt", POSITION_SIZE_USD)) * rem
            pl_usd   = (invested / ep * cp - invested) if ep > 0 and cp > 0 else 0
            cur_val  = (invested / ep * cp) if ep > 0 and cp > 0 else invested
            sg       = "+" if pl_pct >= 0 else ""
            clr      = "profit" if pl_pct >= 0 else "loss"

            # Compact expander label with key info
            exp_label = f"{'🔥' if pl_pct > 50 else '🟢'} **{sym}** · {sg}{pl_pct:.1f}% · {fmt_usd(pl_usd)} · ⏱ {age_h:.1f}h"

            with st.expander(exp_label, expanded=len(positions) <= 4):
                # ── P/L hero ─────────────────────────────────────────────
                st.markdown(
                    f'<div style="display:flex;align-items:baseline;gap:16px;margin-bottom:12px">'
                    f'<span style="font-size:1.6rem;font-weight:800;color:#f1f5f9">{sym}</span>'
                    f'<span class="{clr}" style="font-size:1.4rem;font-weight:800">{sg}{pl_pct:.1f}%</span>'
                    f'<span class="{clr}" style="font-size:1rem">{sg}{fmt_usd(abs(pl_usd))}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # ── Detail grid ──────────────────────────────────────────
                st.markdown(
                    f'<div class="detail-grid">'
                    f'<div class="detail-item"><div class="detail-label">📅 Bought</div><div class="detail-value">{buy_ts}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">💲 Entry</div><div class="detail-value">${ep:.8f}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">💲 Current</div><div class="detail-value">${cp:.8f}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">📈 ATH</div><div class="detail-value">${ath:.8f}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">💰 Invested</div><div class="detail-value">{fmt_usd(invested)}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">💎 Current Val</div><div class="detail-value">{fmt_usd(cur_val)}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">⏱ Age</div><div class="detail-value">{age_h:.1f}h</div></div>'
                    f'<div class="detail-item"><div class="detail-label">📊 Remaining</div><div class="detail-value">{int(rem*100)}%</div></div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # ── Exit levels ──────────────────────────────────────────
                st.markdown(
                    '<span style="color:#7cb4ff;font-size:0.82rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">'
                    '🎯 Exit Levels</span>',
                    unsafe_allow_html=True,
                )
                el1, el2, el3, el4, el5 = st.columns(5)
                sl_price = ep * (1 - STOP_LOSS_PCT)
                tp1_price = ep * (1 + TP1_PCT)
                tp2_price = ep * (1 + TP2_PCT)
                tp3_price = ep * (1 + TP3_PCT)

                el1.markdown(
                    f'<div style="text-align:center;padding:6px;background:rgba(255,92,92,0.08);border:1px solid rgba(255,92,92,0.15);border-radius:8px">'
                    f'<div style="color:#ff5c5c;font-size:0.65rem;font-weight:700">🛑 STOP</div>'
                    f'<div style="color:#ff5c5c;font-size:0.78rem;font-weight:600">${sl_price:.8f}</div>'
                    f'</div>', unsafe_allow_html=True)
                tp1_status = "✅ HIT" if pos.get("tp1_hit") else f"${tp1_price:.8f}"
                tp1_bg = "rgba(0,230,167,0.12)" if pos.get("tp1_hit") else "rgba(0,230,167,0.05)"
                el2.markdown(
                    f'<div style="text-align:center;padding:6px;background:{tp1_bg};border:1px solid rgba(0,230,167,0.15);border-radius:8px">'
                    f'<div style="color:#00e6a7;font-size:0.65rem;font-weight:700">💚 TP1 +{int(TP1_PCT*100)}%</div>'
                    f'<div style="color:#00e6a7;font-size:0.78rem;font-weight:600">{tp1_status}</div>'
                    f'</div>', unsafe_allow_html=True)
                tp2_status = "✅ HIT" if pos.get("tp2_hit") else f"${tp2_price:.8f}"
                tp2_bg = "rgba(0,230,167,0.12)" if pos.get("tp2_hit") else "rgba(0,230,167,0.05)"
                el3.markdown(
                    f'<div style="text-align:center;padding:6px;background:{tp2_bg};border:1px solid rgba(0,230,167,0.15);border-radius:8px">'
                    f'<div style="color:#00e6a7;font-size:0.65rem;font-weight:700">💰 TP2 +{int(TP2_PCT*100)}%</div>'
                    f'<div style="color:#00e6a7;font-size:0.78rem;font-weight:600">{tp2_status}</div>'
                    f'</div>', unsafe_allow_html=True)
                el4.markdown(
                    f'<div style="text-align:center;padding:6px;background:rgba(0,230,167,0.05);border:1px solid rgba(0,230,167,0.15);border-radius:8px">'
                    f'<div style="color:#00e6a7;font-size:0.65rem;font-weight:700">🚀 TP3 +{int(TP3_PCT*100)}%</div>'
                    f'<div style="color:#00e6a7;font-size:0.78rem;font-weight:600">${tp3_price:.8f}</div>'
                    f'</div>', unsafe_allow_html=True)
                trail_status = "✅ ACTIVE" if trail else f"at +{int(TRAILING_ACTIVATE*100)}%"
                trail_bg = "rgba(167,139,250,0.12)" if trail else "rgba(167,139,250,0.05)"
                el5.markdown(
                    f'<div style="text-align:center;padding:6px;background:{trail_bg};border:1px solid rgba(167,139,250,0.15);border-radius:8px">'
                    f'<div style="color:#a78bfa;font-size:0.65rem;font-weight:700">📉 TRAILING</div>'
                    f'<div style="color:#a78bfa;font-size:0.78rem;font-weight:600">{trail_status}</div>'
                    f'</div>', unsafe_allow_html=True)

                # ── Market data + Sell button ────────────────────────────
                info = get_token_full_info(addr)
                data_col, action_col = st.columns([3, 1])

                with data_col:
                    if info:
                        st.markdown(
                            f'<div style="display:flex;gap:20px;flex-wrap:wrap;padding:8px 0;border-top:1px solid #151b27;margin-top:8px">'
                            f'<span style="color:#7cb4ff;font-size:0.85rem">24h Vol: <span style="color:#ffffff">{fmt_usd(info.get("volume_24h", 0))}</span></span>'
                            f'<span style="color:#7cb4ff;font-size:0.85rem">24h Change: <span style="color:#ffffff">{fmt_pct(info.get("change_24h", 0))}</span></span>'
                            f'<span style="color:#7cb4ff;font-size:0.85rem">24h High: <span style="color:#ffffff">${info.get("high_24h", 0):.4f}</span></span>'
                            f'<span style="color:#7cb4ff;font-size:0.85rem">24h Low: <span style="color:#ffffff">${info.get("low_24h", 0):.4f}</span></span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

                    # Links
                    binance_sym = addr.replace("USDT", "_USDT")
                    st.markdown(
                        f'<div style="display:flex;gap:12px;margin-top:4px">'
                        f'<a href="https://www.binance.com/en/trade/{binance_sym}?type=spot" target="_blank" style="color:#3b8bff;font-size:0.88rem;font-weight:600;text-decoration:none">📊 Open in Binance</a>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                with action_col:
                    st.markdown(
                        f'<div style="text-align:center;padding:12px;background:rgba(59,139,255,0.05);border:1px solid rgba(59,139,255,0.15);border-radius:8px">'
                        f'<div style="color:#7cb4ff;font-size:0.75rem;font-weight:700">🤖 AUTO-MANAGED</div>'
                        f'<div style="color:#94a3b8;font-size:0.7rem;margin-top:4px">Binance OCO aktiv</div>'
                        f'</div>', unsafe_allow_html=True
                    )

    _render_open()
