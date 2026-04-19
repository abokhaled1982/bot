import time
import streamlit as st
from dashboard.config import (
    WALLET_ADDRESS, POSITION_SIZE_USD, STOP_LOSS_PCT,
    TRAILING_ACTIVATE, TP1_PCT, TP2_PCT, TP3_PCT, MAX_HOLD_HOURS,
)
from dashboard.db import (
    db_query, get_live_price, get_reconciled_positions,
    get_sol_price_and_change, get_btc_price_and_change,
    get_wallet_sol_balance, get_recent_events,
)
from dashboard.components import fmt_usd, fmt_pct, kpi_card

_EVENT_ICONS = {
    "BUY_SUCCESS":        "🟢",
    "BUY_SIMULATED":      "🟡",
    "BUY_FAILED":         "❌",
    "SELL_SUCCESS":       "🔴",
    "SELL_SIMULATED":     "🟠",
    "SELL_FAILED":        "⚠️",
    "SELL_TP1":           "💚",
    "SELL_TP2":           "💰",
    "SELL_TP3":           "🚀",
    "SELL_STOP_LOSS":     "🛑",
    "SELL_TRAILING_STOP": "📉",
    "SELL_TIME_EXIT":     "⏰",
    "SELL_MANUAL":        "🖱️",
    "BOT_START":          "▶️",
    "BOT_STOP":           "⏹️",
    "POSITION_ADDED":     "📌",
    "POSITION_CLOSED":    "✅",
    "BUY":                "🟢",
    "SELL":               "🔴",
    "REJECT":             "⛔",
}


def render():
    @st.fragment(run_every="10s")
    def _render():
        positions = get_reconciled_positions(WALLET_ADDRESS)

        total_pl_usd   = 0.0
        total_invested = 0.0
        total_current  = 0.0

        for addr, pos in positions.items():
            ep  = float(pos.get("entry_price", 0))
            rem = float(pos.get("remaining_pct", 1.0))
            cp  = get_live_price(addr)
            inv = POSITION_SIZE_USD * rem
            cur = (inv / ep * cp) if ep > 0 and cp > 0 else inv
            total_invested += inv
            total_current  += cur
            total_pl_usd   += cur - inv

        pl_pct = (total_pl_usd / total_invested * 100) if total_invested > 0 else 0

        # ── KPI row ───────────────────────────────────────────────────────────
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.markdown(kpi_card("Open Positions", str(len(positions)), "active"), unsafe_allow_html=True)
        c2.markdown(kpi_card("Invested",       fmt_usd(total_invested), "live"),  unsafe_allow_html=True)
        c3.markdown(kpi_card("Current Value",  fmt_usd(total_current),  "live"),  unsafe_allow_html=True)
        pl_html = f'<span class="{"profit" if total_pl_usd >= 0 else "loss"}">{fmt_usd(total_pl_usd)}</span>'
        c4.markdown(kpi_card("Unrealized P/L", pl_html, fmt_pct(pl_pct)), unsafe_allow_html=True)

        df_pl = db_query("""
            SELECT
                SUM(CASE WHEN decision='BUY'  THEN buy_amount_usd  ELSE 0 END) AS invested,
                SUM(CASE WHEN decision='SELL' THEN sell_amount_usd ELSE 0 END) AS returned
            FROM trades
        """)
        if not df_pl.empty:
            inv = float(df_pl.iloc[0]["invested"] or 0)
            ret = float(df_pl.iloc[0]["returned"] or 0)
            net = ret - inv
            net_html = f'<span class="{"profit" if net >= 0 else "loss"}">{fmt_usd(net)}</span>'
            c5.markdown(kpi_card("Realized P/L", net_html, "all-time"), unsafe_allow_html=True)

        st.markdown("")

        # ── Active positions mini-grid ────────────────────────────────────────
        if positions:
            st.markdown('<div class="section-header" style="font-size:1rem">🟢 Live Positions</div>', unsafe_allow_html=True)
            for addr, pos in list(positions.items())[:10]:
                ep   = float(pos.get("entry_price", 0))
                sym  = pos.get("symbol", addr[:8])
                rem  = float(pos.get("remaining_pct", 1.0))
                cp   = get_live_price(addr)
                pl_p = ((cp - ep) / ep * 100) if ep > 0 and cp > 0 else 0
                pl_u = (POSITION_SIZE_USD * rem / ep * cp - POSITION_SIZE_USD * rem) if ep > 0 and cp > 0 else 0
                age_h = (time.time() - float(pos.get("created_at", time.time()))) / 3600
                clr  = "profit" if pl_p >= 0 else "loss"
                sg   = "+" if pl_p >= 0 else ""
                tp1  = "✅" if pos.get("tp1_hit") else f"${ep*(1+TP1_PCT):.8f}"
                tp2  = "✅" if pos.get("tp2_hit") else f"${ep*(1+TP2_PCT):.8f}"
                sl   = f"${ep*(1-STOP_LOSS_PCT):.8f}"
                st.markdown(
                    f'<div style="display:flex;justify-content:space-between;align-items:center;'
                    f'padding:10px 14px;background:#111319;border:1px solid #1e293b;border-radius:6px;margin-bottom:6px">'
                    f'<span style="font-weight:700;color:#fff;font-size:1rem">{sym}</span>'
                    f'<span style="color:#5eead4;font-size:0.88rem">${ep:.8f} → ${cp:.8f}</span>'
                    f'<span class="{clr}">{sg}{pl_p:.1f}% &nbsp;{fmt_usd(pl_u)}</span>'
                    f'<span style="color:#e0a846;font-size:0.85rem">TP1:{tp1} &nbsp; TP2:{tp2} &nbsp; SL:{sl}</span>'
                    f'<span style="color:#c4b5fd;font-size:0.85rem">⏱{age_h:.1f}h &nbsp; {int(rem*100)}% left</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.info("No open positions.")

        # ── Structured event feed ─────────────────────────────────────────────
        st.markdown("")
        st.markdown('<div class="section-header" style="font-size:0.9rem">⚡ Live Event Feed</div>', unsafe_allow_html=True)
        df_events = get_recent_events(limit=20)

        if df_events.empty:
            st.caption("No events recorded yet.")
        else:
            for _, ev in df_events.iterrows():
                et   = str(ev.get("event_type", ""))
                sym  = str(ev.get("symbol", "") or "?")
                icon = _EVENT_ICONS.get(et, "🔵")
                ts   = str(ev.get("timestamp", "") or "")[:16]
                msg  = str(ev.get("message", "") or "")[:80]
                addr = str(ev.get("address", "") or "")

                # P/L badge
                pnl_badge = ""
                pnl_usd = ev.get("pnl_usd")
                pnl_pct = ev.get("pnl_pct")
                if pnl_usd is not None:
                    clr    = "#22c55e" if float(pnl_usd) >= 0 else "#ef4444"
                    sg     = "+" if float(pnl_usd) >= 0 else ""
                    pp     = f" ({sg}{float(pnl_pct)*100:.1f}%)" if pnl_pct is not None else ""
                    pnl_badge = (
                        f'&nbsp;<span style="color:{clr};font-weight:600">'
                        f'{sg}{float(pnl_usd):.4f}${pp}</span>'
                    )

                # TX link
                tx_link = ""
                tx = str(ev.get("tx_signature", "") or "")
                if tx and len(tx) > 10:
                    tx_link = f' &nbsp;<a href="https://solscan.io/tx/{tx}" target="_blank" style="color:#60a5fa;font-size:0.75rem">🔗 tx</a>'

                # Token link
                tok_link = ""
                if addr and len(addr) > 10:
                    tok_link = f' <a href="https://dexscreener.com/solana/{addr}" target="_blank" style="color:#3b8bff;font-size:0.85rem">📊</a>'

                st.markdown(
                    f'<div style="display:flex;gap:10px;align-items:flex-start;'
                    f'padding:6px 10px;border-bottom:1px solid #1e293b;">'
                    f'<span style="font-size:1rem;min-width:22px">{icon}</span>'
                    f'<span style="color:#5eead4;font-size:0.85rem;min-width:105px">{ts}</span>'
                    f'<span style="font-weight:700;color:#fff;min-width:70px">{sym}{tok_link}</span>'
                    f'<span style="color:#e0a846;font-size:0.88rem;font-family:monospace">{et}</span>'
                    f'{pnl_badge}{tx_link}'
                    f'<span style="color:#c4b5fd;font-size:0.85rem;margin-left:auto">{msg}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    _render()
    @st.fragment(run_every="10s")
    def _render():
        positions = get_reconciled_positions(WALLET_ADDRESS)

        total_pl_usd     = 0.0
        total_invested   = 0.0
        total_current    = 0.0

        for addr, pos in positions.items():
            ep  = float(pos.get("entry_price", 0))
            rem = float(pos.get("remaining_pct", 1.0))
            cp  = get_live_price(addr)
            invested  = POSITION_SIZE_USD * rem
            cur_val   = (invested / ep * cp) if ep > 0 and cp > 0 else invested
            total_invested += invested
            total_current  += cur_val
            total_pl_usd   += cur_val - invested

        pl_pct = (total_pl_usd / total_invested * 100) if total_invested > 0 else 0

        # ── KPI row ───────────────────────────────────────────────────────────
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.markdown(kpi_card("Open Positions", str(len(positions)), "active"), unsafe_allow_html=True)
        c2.markdown(kpi_card("Invested", fmt_usd(total_invested), "live"), unsafe_allow_html=True)
        c3.markdown(kpi_card("Current Value", fmt_usd(total_current), "live"), unsafe_allow_html=True)
        pl_html = f'<span class="{"profit" if total_pl_usd >= 0 else "loss"}">{fmt_usd(total_pl_usd)}</span>'
        c4.markdown(kpi_card("Unrealized P/L", pl_html, f"{fmt_pct(pl_pct)}"), unsafe_allow_html=True)

        # DB realized P/L
        df_pl = db_query("""
            SELECT
                SUM(CASE WHEN decision='BUY'  THEN buy_amount_usd  ELSE 0 END) as invested,
                SUM(CASE WHEN decision='SELL' THEN sell_amount_usd ELSE 0 END) as returned
            FROM trades
        """)
        if not df_pl.empty:
            inv = float(df_pl.iloc[0]["invested"]  or 0)
            ret = float(df_pl.iloc[0]["returned"]  or 0)
            net = ret - inv
            net_html = f'<span class="{"profit" if net >= 0 else "loss"}">{fmt_usd(net)}</span>'
            c5.markdown(kpi_card("Realized P/L", net_html, "all-time"), unsafe_allow_html=True)

        st.markdown("")

        # ── Active positions mini-grid ────────────────────────────────────────
        if not positions:
            st.info("No open positions.")
            return

        st.markdown('<div class="section-header" style="font-size:0.9rem">🟢 Live Positions</div>', unsafe_allow_html=True)
        for addr, pos in list(positions.items())[:10]:
            ep    = float(pos.get("entry_price", 0))
            sym   = pos.get("symbol", addr[:8])
            rem   = float(pos.get("remaining_pct", 1.0))
            cp    = get_live_price(addr)
            pl_p  = ((cp - ep) / ep * 100) if ep > 0 and cp > 0 else 0
            pl_u  = (POSITION_SIZE_USD * rem / ep * cp - POSITION_SIZE_USD * rem) if ep > 0 and cp > 0 else 0
            age_h = (time.time() - float(pos.get("created_at", time.time()))) / 3600
            clr   = "profit" if pl_p >= 0 else "loss"
            sg    = "+" if pl_p >= 0 else ""

            tp1 = "✅" if pos.get("tp1_hit") else f"${ep*(1+TP1_PCT):.8f}"
            tp2 = "✅" if pos.get("tp2_hit") else f"${ep*(1+TP2_PCT):.8f}"
            sl  = f"${ep*(1-STOP_LOSS_PCT):.8f}"

            st.markdown(
                f'<div style="display:flex;justify-content:space-between;align-items:center;'
                f'padding:10px 14px;background:#111319;border:1px solid #1e293b;border-radius:6px;margin-bottom:6px">'
                f'<span style="font-weight:700;color:#fff;font-size:1rem">{sym}</span>'
                f'<span style="color:#5eead4;font-size:0.88rem">${ep:.8f} → ${cp:.8f}</span>'
                f'<span class="{clr}">{sg}{pl_p:.1f}% &nbsp;{fmt_usd(pl_u)}</span>'
                f'<span style="color:#e0a846;font-size:0.85rem">TP1:{tp1} &nbsp; TP2:{tp2} &nbsp; SL:{sl}</span>'
                f'<span style="color:#c4b5fd;font-size:0.85rem">⏱{age_h:.1f}h &nbsp; {int(rem*100)}% left</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ── Recent events ────────────────────────────────────────────────────
        st.markdown("")
        st.markdown('<div class="section-header" style="font-size:0.9rem">⚡ Recent Events</div>', unsafe_allow_html=True)
        df_events = db_query("""
            SELECT symbol, decision, entry_price, rejection_reason, timestamp, funnel_stage
            FROM trades
            WHERE decision IN ('BUY','SELL','SELL_FAILED','ERROR','BUY_UNCONFIRMED')
            ORDER BY timestamp DESC LIMIT 15
        """)
        if not df_events.empty:
            for _, row in df_events.iterrows():
                dec  = str(row["decision"])
                icon = {"BUY": "🟢", "SELL": "🔴", "SELL_FAILED": "⚠️", "ERROR": "❌", "BUY_UNCONFIRMED": "⚠️"}.get(dec, "🔵")
                ep   = float(row["entry_price"] or 0)
                ts   = str(row["timestamp"] or "")[:16]
                st.caption(f"{icon} {ts}  **{row['symbol']}**  {dec}  @ ${ep:.8f}  — {str(row['rejection_reason'] or '')[:60]}")

    _render()
