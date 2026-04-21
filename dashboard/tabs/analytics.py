import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import numpy as np
from dashboard.db import (
    db_query, get_live_price, get_wallet_sol_balance,
    get_wallet_tokens, get_sol_price_and_change, get_token_full_info,
)
from dashboard.components import fmt_usd, fmt_pct, kpi_card
from dashboard.config import WALLET_ADDRESS, POSITION_SIZE_USD

_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#0c0f16",
    font=dict(color="#e2e8f0", family="Inter, sans-serif", size=13),
    margin=dict(l=30, r=20, t=40, b=30),
    xaxis=dict(gridcolor="#151b27", zeroline=False),
    yaxis=dict(gridcolor="#151b27", zeroline=False),
    hoverlabel=dict(bgcolor="#111827", font_color="#e2e8f0", bordercolor="#1e2536"),
)

_COLORS = {
    "green":  "#00e6a7", "red": "#ff5c5c", "blue": "#3b8bff",
    "amber":  "#ffb400", "purple": "#a78bfa", "muted": "#7cb4ff",
    "cyan":   "#22d3ee", "pink": "#f472b6", "lime": "#84cc16",
}

def _layout(**overrides):
    """Merge _LAYOUT with overrides without duplicate-key errors."""
    base = dict(_LAYOUT)
    for k, v in overrides.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


def render():

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 1 — Wallet Overview
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown('<div class="section-header">💰 Wallet Overview</div>', unsafe_allow_html=True)

    @st.fragment(run_every="30s")
    def _wallet():
        sol_bal            = get_wallet_sol_balance(WALLET_ADDRESS)
        sol_price, sol_chg = get_sol_price_and_change()
        sol_usd            = sol_bal * sol_price
        tokens             = get_wallet_tokens(WALLET_ADDRESS)

        # Token value calculation
        total_token_usd = 0.0
        for t in tokens:
            cp = get_live_price(t["mint"])
            total_token_usd += t["amount"] * cp if cp > 0 else 0
        total_wallet_usd = sol_usd + total_token_usd

        wc1, wc2, wc3, wc4, wc5 = st.columns(5)
        wc1.markdown(kpi_card("Total Wallet", fmt_usd(total_wallet_usd), "SOL + tokens"), unsafe_allow_html=True)
        wc2.markdown(kpi_card("SOL Balance", f"{sol_bal:.4f}", fmt_usd(sol_usd)), unsafe_allow_html=True)
        wc3.markdown(kpi_card("SOL Price", fmt_usd(sol_price), fmt_pct(sol_chg)), unsafe_allow_html=True)
        wc4.markdown(kpi_card("Token Holdings", fmt_usd(total_token_usd), f"{len(tokens)} tokens"), unsafe_allow_html=True)

        # P/L summary
        df_pl = db_query("""
            SELECT
                SUM(CASE WHEN decision LIKE '%BUY%'  THEN buy_amount_usd  ELSE 0 END) as invested,
                SUM(CASE WHEN decision LIKE '%SELL%' THEN sell_amount_usd ELSE 0 END) as returned
            FROM trades
        """)
        if not df_pl.empty:
            inv = float(df_pl.iloc[0]["invested"] or 0)
            ret = float(df_pl.iloc[0]["returned"] or 0)
            net = ret - inv
            net_html = f'<span class="{"profit" if net >= 0 else "loss"}">{fmt_usd(net)}</span>'
            wc5.markdown(kpi_card("Net Realized P/L", net_html, "all-time"), unsafe_allow_html=True)

        # Token list
        if tokens:
            st.markdown("")
            st.markdown(
                '<span style="color:#7cb4ff;font-size:0.75rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">'
                '🏦 Token Holdings</span>',
                unsafe_allow_html=True,
            )
            for t in tokens:
                cp      = get_live_price(t["mint"])
                val_usd = t["amount"] * cp if cp > 0 else 0
                st.markdown(
                    f'<div style="display:flex;justify-content:space-between;align-items:center;padding:10px 14px;'
                    f'background:#0c0f16;border:1px solid #151b27;border-radius:8px;margin-bottom:4px">'
                    f'<span style="font-weight:700;color:#f1f5f9;min-width:90px">{t["symbol"]}</span>'
                    f'<span style="color:#7cb4ff;font-size:0.82rem;font-family:JetBrains Mono,monospace">{t["amount"]:.4f}</span>'
                    f'<span style="color:#7cb4ff;font-size:0.82rem;font-family:JetBrains Mono,monospace">${cp:.8f}</span>'
                    f'<span style="color:#e2e8f0;font-weight:600">{fmt_usd(val_usd)}</span>'
                    f'<a href="https://dexscreener.com/solana/{t["mint"]}" target="_blank" '
                    f'style="color:#3b8bff;font-size:0.78rem;text-decoration:none">📊</a>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        st.markdown(
            f'<div style="margin-top:8px">'
            f'<a href="https://solscan.io/account/{WALLET_ADDRESS}" target="_blank" '
            f'style="color:#3b8bff;font-size:0.8rem;text-decoration:none">🔗 View on Solscan</a>'
            f'<span style="color:#5eead4;font-size:0.75rem;margin-left:12px;font-family:JetBrains Mono,monospace">{WALLET_ADDRESS[:20]}...</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    _wallet()

    st.markdown("---")

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 2 — Trade Performance Analytics
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown('<div class="section-header">📊 Trade Performance</div>', unsafe_allow_html=True)

    # ── Build trade-level P/L dataset ─────────────────────────────────────────
    df_trades = db_query("""
        SELECT
            t_sell.token_address, t_sell.symbol,
            t_buy.entry_price  AS buy_price,
            t_sell.entry_price AS sell_price,
            t_buy.buy_amount_usd,
            t_sell.sell_amount_usd,
            t_buy.timestamp    AS buy_time,
            t_sell.timestamp   AS sell_time,
            t_sell.funnel_stage AS exit_type,
            t_buy.score
        FROM trades t_sell
        JOIN trades t_buy
          ON t_sell.token_address = t_buy.token_address AND t_buy.decision LIKE '%BUY%'
        WHERE t_sell.decision LIKE '%SELL%'
          AND t_sell.entry_price > 0 AND t_buy.entry_price > 0
        ORDER BY t_sell.timestamp
    """)

    # Enrich with P/L from bot_events where available
    df_events_pnl = db_query("""
        SELECT address, pnl_usd, pnl_pct, event_type, timestamp
        FROM bot_events
        WHERE pnl_usd IS NOT NULL
        ORDER BY timestamp
    """)

    has_trades = not df_trades.empty

    if has_trades:
        df_trades["pnl_usd"] = df_trades["sell_amount_usd"] - df_trades["buy_amount_usd"]
        df_trades["pnl_pct"] = (
            (df_trades["sell_price"] - df_trades["buy_price"]) / df_trades["buy_price"] * 100
        )
        df_trades["is_win"] = df_trades["pnl_pct"] > 0
        df_trades["result"] = df_trades["is_win"].map({True: "Win", False: "Loss"})

        # Merge event-level pnl if available (more accurate)
        if not df_events_pnl.empty:
            ev_map = df_events_pnl.groupby("address").first()
            for idx, row in df_trades.iterrows():
                addr = row["token_address"]
                if addr in ev_map.index:
                    df_trades.at[idx, "pnl_usd"] = float(ev_map.loc[addr, "pnl_usd"])
                    df_trades.at[idx, "pnl_pct"] = float(ev_map.loc[addr, "pnl_pct"]) * 100
                    df_trades.at[idx, "is_win"] = float(ev_map.loc[addr, "pnl_usd"]) > 0
                    df_trades.at[idx, "result"] = "Win" if float(ev_map.loc[addr, "pnl_usd"]) > 0 else "Loss"

        # Calculate hold time in hours
        try:
            df_trades["buy_dt"] = pd.to_datetime(df_trades["buy_time"])
            df_trades["sell_dt"] = pd.to_datetime(df_trades["sell_time"])
            df_trades["hold_hours"] = (
                (df_trades["sell_dt"] - df_trades["buy_dt"]).dt.total_seconds() / 3600
            )
        except Exception:
            df_trades["hold_hours"] = 0

        total_trades = len(df_trades)
        wins = int(df_trades["is_win"].sum())
        losses = total_trades - wins
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        total_pnl = df_trades["pnl_usd"].sum()
        avg_win = df_trades.loc[df_trades["is_win"], "pnl_usd"].mean() if wins > 0 else 0
        avg_loss = df_trades.loc[~df_trades["is_win"], "pnl_usd"].mean() if losses > 0 else 0
        gross_wins = df_trades.loc[df_trades["is_win"], "pnl_usd"].sum()
        gross_losses = abs(df_trades.loc[~df_trades["is_win"], "pnl_usd"].sum())
        profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else float("inf")
        best_trade = df_trades["pnl_pct"].max()
        worst_trade = df_trades["pnl_pct"].min()
        best_sym = df_trades.loc[df_trades["pnl_pct"].idxmax(), "symbol"] if total_trades > 0 else "—"
        worst_sym = df_trades.loc[df_trades["pnl_pct"].idxmin(), "symbol"] if total_trades > 0 else "—"

        # ── Hero KPIs ─────────────────────────────────────────────────────────
        k1, k2, k3, k4, k5, k6, k7 = st.columns(7)
        wr_clr = "profit" if win_rate >= 50 else "loss"
        pnl_clr = "profit" if total_pnl >= 0 else "loss"
        pf_clr = "profit" if profit_factor >= 1 else "loss"
        k1.markdown(kpi_card("Win Rate", f'<span class="{wr_clr}">{win_rate:.1f}%</span>', f"{wins}W / {losses}L"), unsafe_allow_html=True)
        k2.markdown(kpi_card("Total P/L", f'<span class="{pnl_clr}">{fmt_usd(total_pnl)}</span>', f"{total_trades} trades"), unsafe_allow_html=True)
        k3.markdown(kpi_card("Profit Factor", f'<span class="{pf_clr}">{profit_factor:.2f}</span>', "wins / losses"), unsafe_allow_html=True)
        k4.markdown(kpi_card("Avg Win", f'<span class="profit">{fmt_usd(avg_win)}</span>', f"{wins} trades"), unsafe_allow_html=True)
        k5.markdown(kpi_card("Avg Loss", f'<span class="loss">{fmt_usd(avg_loss)}</span>', f"{losses} trades"), unsafe_allow_html=True)
        k6.markdown(kpi_card("Best Trade", f'<span class="profit">+{best_trade:.1f}%</span>', best_sym), unsafe_allow_html=True)
        k7.markdown(kpi_card("Worst Trade", f'<span class="loss">{worst_trade:.1f}%</span>', worst_sym), unsafe_allow_html=True)

        st.markdown("")

        # ══════════════════════════════════════════════════════════════════════
        # ROW 1 — Win Rate Ring + Equity Curve
        # ══════════════════════════════════════════════════════════════════════
        ring_col, equity_col = st.columns([1, 2])

        with ring_col:
            # Donut chart with center percentage
            fig_ring = go.Figure()
            fig_ring.add_trace(go.Pie(
                values=[wins, losses],
                labels=["Wins", "Losses"],
                hole=0.72,
                marker=dict(
                    colors=[_COLORS["green"], _COLORS["red"]],
                    line=dict(color="#0c0f16", width=3),
                ),
                textinfo="label+value",
                textfont=dict(size=13, color="#e2e8f0"),
                hovertemplate="<b>%{label}</b><br>%{value} trades<br>%{percent}<extra></extra>",
                sort=False,
            ))
            fig_ring.update_layout(
                _layout(
                    height=320, showlegend=False,
                    margin=dict(l=20, r=20, t=20, b=20),
                    annotations=[
                        dict(
                            text=f'<b style="font-size:2rem;color:#f1f5f9">{win_rate:.0f}%</b><br>'
                                 f'<span style="font-size:0.75rem;color:#7cb4ff">WIN RATE</span>',
                            x=0.5, y=0.5, font_size=14, showarrow=False,
                            font=dict(color="#f1f5f9"),
                        ),
                    ],
                )
            )
            st.plotly_chart(fig_ring, use_container_width=True)

            # Win/Loss streak
            streaks = df_trades["is_win"].tolist()
            current_streak = 0
            max_win_streak = 0
            max_loss_streak = 0
            cw = 0
            cl = 0
            for w in streaks:
                if w:
                    cw += 1
                    cl = 0
                    max_win_streak = max(max_win_streak, cw)
                else:
                    cl += 1
                    cw = 0
                    max_loss_streak = max(max_loss_streak, cl)
            last_is_win = streaks[-1] if streaks else True
            current_streak = cw if last_is_win else cl
            streak_label = "W" if last_is_win else "L"
            streak_clr = "#00e6a7" if last_is_win else "#ff5c5c"

            st.markdown(
                f'<div style="display:flex;gap:8px;justify-content:center;margin-top:-8px">'
                f'<div style="text-align:center;padding:10px 16px;background:#0c0f16;border:1px solid #151b27;border-radius:10px;flex:1">'
                f'<div style="color:#e0a846;font-size:0.62rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">Current</div>'
                f'<div style="color:{streak_clr};font-size:1.3rem;font-weight:800">{current_streak}{streak_label}</div>'
                f'</div>'
                f'<div style="text-align:center;padding:10px 16px;background:#0c0f16;border:1px solid #151b27;border-radius:10px;flex:1">'
                f'<div style="color:#e0a846;font-size:0.62rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">Best Streak</div>'
                f'<div style="color:#00e6a7;font-size:1.3rem;font-weight:800">{max_win_streak}W</div>'
                f'</div>'
                f'<div style="text-align:center;padding:10px 16px;background:#0c0f16;border:1px solid #151b27;border-radius:10px;flex:1">'
                f'<div style="color:#e0a846;font-size:0.62rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">Worst Streak</div>'
                f'<div style="color:#ff5c5c;font-size:1.3rem;font-weight:800">{max_loss_streak}L</div>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        with equity_col:
            # Cumulative P/L equity curve
            df_eq = df_trades[["sell_time", "pnl_usd", "symbol"]].copy()
            df_eq["cumulative"] = df_eq["pnl_usd"].cumsum()

            fig_eq = go.Figure()
            # Area fill split into profit/loss zones
            fig_eq.add_trace(go.Scatter(
                x=list(range(len(df_eq))),
                y=df_eq["cumulative"],
                mode="lines+markers",
                line=dict(color=_COLORS["green"] if df_eq["cumulative"].iloc[-1] >= 0 else _COLORS["red"], width=2.5, shape="spline"),
                marker=dict(
                    size=8,
                    color=[_COLORS["green"] if v >= 0 else _COLORS["red"] for v in df_eq["pnl_usd"]],
                    line=dict(width=2, color="#0c0f16"),
                ),
                fill="tozeroy",
                fillcolor="rgba(0,230,167,0.06)" if df_eq["cumulative"].iloc[-1] >= 0 else "rgba(255,92,92,0.06)",
                name="Equity",
                customdata=df_eq[["symbol", "pnl_usd"]].values,
                hovertemplate="<b>Trade #%{x}</b> — %{customdata[0]}<br>Trade P/L: $%{customdata[1]:.4f}<br>Cumulative: $%{y:.4f}<extra></extra>",
            ))
            fig_eq.add_hline(y=0, line_dash="dash", line_color="#5eead4", line_width=1)

            # High-water mark line
            hwm = df_eq["cumulative"].cummax()
            fig_eq.add_trace(go.Scatter(
                x=list(range(len(df_eq))),
                y=hwm,
                mode="lines",
                line=dict(color=_COLORS["amber"], width=1, dash="dot"),
                name="High Water Mark",
                hoverinfo="skip",
            ))
            fig_eq.update_layout(
                _layout(
                    height=320,
                    title=dict(text="Equity Curve (Cumulative P/L)", font=dict(size=14, color="#c4b5fd")),
                    xaxis=dict(title="Trade #", gridcolor="#151b27", zeroline=False),
                    yaxis=dict(title="P/L ($)", gridcolor="#151b27", zeroline=False),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, bgcolor="rgba(0,0,0,0)"),
                )
            )
            st.plotly_chart(fig_eq, use_container_width=True)

        # ══════════════════════════════════════════════════════════════════════
        # ROW 2 — Individual Trade P/L Bars + Daily Performance
        # ══════════════════════════════════════════════════════════════════════
        bar_col, daily_col = st.columns(2)

        with bar_col:
            # Per-trade P/L waterfall bars
            fig_bars = go.Figure()
            colors = [_COLORS["green"] if v > 0 else _COLORS["red"] for v in df_trades["pnl_pct"]]
            fig_bars.add_trace(go.Bar(
                x=df_trades["symbol"],
                y=df_trades["pnl_pct"],
                marker=dict(
                    color=colors,
                    line=dict(width=0),
                    opacity=0.9,
                ),
                customdata=df_trades[["pnl_usd", "exit_type"]].values,
                hovertemplate="<b>%{x}</b><br>P/L: %{y:.1f}%<br>$%{customdata[0]:.4f}<br>Exit: %{customdata[1]}<extra></extra>",
            ))
            fig_bars.add_hline(y=0, line_color="#5eead4", line_width=1)
            fig_bars.update_layout(
                _layout(
                    height=300, showlegend=False,
                    title=dict(text="P/L Per Trade (%)", font=dict(size=13, color="#c4b5fd")),
                    xaxis=dict(title="", gridcolor="#151b27", zeroline=False, tickangle=-45),
                    yaxis=dict(title="P/L %", gridcolor="#151b27", zeroline=False),
                )
            )
            st.plotly_chart(fig_bars, use_container_width=True)

        with daily_col:
            # Daily P/L aggregated
            df_daily = db_query("""
                SELECT
                    DATE(timestamp) as date,
                    SUM(CASE WHEN decision='BUY'  THEN -buy_amount_usd  ELSE 0 END) +
                    SUM(CASE WHEN decision='SELL' THEN sell_amount_usd ELSE 0 END) as daily_pnl
                FROM trades
                WHERE decision IN ('BUY', 'SELL') AND timestamp IS NOT NULL
                GROUP BY DATE(timestamp)
                ORDER BY date
            """)
            if not df_daily.empty:
                daily_colors = [_COLORS["green"] if v > 0 else _COLORS["red"] for v in df_daily["daily_pnl"]]
                fig_daily = go.Figure()
                fig_daily.add_trace(go.Bar(
                    x=df_daily["date"],
                    y=df_daily["daily_pnl"],
                    marker=dict(color=daily_colors, opacity=0.9, line=dict(width=0)),
                    hovertemplate="<b>%{x}</b><br>P/L: $%{y:.4f}<extra></extra>",
                ))
                fig_daily.add_hline(y=0, line_color="#5eead4", line_width=1)
                fig_daily.update_layout(
                    _layout(
                        height=300, showlegend=False,
                        title=dict(text="Daily P/L ($)", font=dict(size=13, color="#c4b5fd")),
                        xaxis=dict(title="", gridcolor="#151b27", zeroline=False),
                        yaxis=dict(title="P/L ($)", gridcolor="#151b27", zeroline=False),
                    )
                )
                st.plotly_chart(fig_daily, use_container_width=True)

        # ══════════════════════════════════════════════════════════════════════
        # ROW 3 — Exit Strategy Analysis + P/L Distribution
        # ══════════════════════════════════════════════════════════════════════
        exit_col, dist_col = st.columns(2)

        with exit_col:
            # Exit strategy performance (TP1, TP2, TP3, SL, Trailing, etc.)
            exit_map = {
                "TP1": "💚 TP1", "TP2": "💰 TP2", "TP3": "🚀 TP3",
                "STOP_LOSS": "🛑 Stop Loss", "TRAILING_STOP": "📉 Trailing",
                "MANUAL": "🖱️ Manual", "SELL_EXEC": "⚡ Exec",
                "FINAL": "⏰ Time Exit",
            }
            exit_colors = {
                "💚 TP1": "#00e6a7", "💰 TP2": "#22d3ee", "🚀 TP3": "#3b8bff",
                "🛑 Stop Loss": "#ff5c5c", "📉 Trailing": "#a78bfa",
                "🖱️ Manual": "#ffb400", "⚡ Exec": "#e0a846", "⏰ Time Exit": "#f472b6",
            }
            df_exit = df_trades.copy()
            df_exit["exit_label"] = df_exit["exit_type"].map(exit_map).fillna("Other")
            df_exit_grp = df_exit.groupby("exit_label").agg(
                count=("pnl_usd", "size"),
                avg_pnl=("pnl_pct", "mean"),
                total_pnl=("pnl_usd", "sum"),
            ).reset_index().sort_values("count", ascending=True)

            fig_exit = go.Figure()
            bar_colors = [exit_colors.get(l, _COLORS["muted"]) for l in df_exit_grp["exit_label"]]
            fig_exit.add_trace(go.Bar(
                y=df_exit_grp["exit_label"],
                x=df_exit_grp["count"],
                orientation="h",
                marker=dict(color=bar_colors, opacity=0.9),
                customdata=df_exit_grp[["avg_pnl", "total_pnl"]].values,
                hovertemplate="<b>%{y}</b><br>Trades: %{x}<br>Avg P/L: %{customdata[0]:.1f}%<br>Total: $%{customdata[1]:.4f}<extra></extra>",
            ))
            fig_exit.update_layout(
                _layout(
                    height=300, showlegend=False,
                    title=dict(text="Exit Strategy Breakdown", font=dict(size=13, color="#c4b5fd")),
                    xaxis=dict(title="Trades", gridcolor="#151b27", zeroline=False),
                    yaxis=dict(title="", gridcolor="#151b27", zeroline=False, categoryorder="total ascending"),
                )
            )
            st.plotly_chart(fig_exit, use_container_width=True)

        with dist_col:
            # P/L distribution histogram with gradient
            fig_dist = go.Figure()
            win_data = df_trades.loc[df_trades["is_win"], "pnl_pct"]
            loss_data = df_trades.loc[~df_trades["is_win"], "pnl_pct"]

            if not loss_data.empty:
                fig_dist.add_trace(go.Histogram(
                    x=loss_data, nbinsx=15, name="Losses",
                    marker=dict(color="rgba(255,92,92,0.7)", line=dict(width=1, color="#ff5c5c")),
                ))
            if not win_data.empty:
                fig_dist.add_trace(go.Histogram(
                    x=win_data, nbinsx=15, name="Wins",
                    marker=dict(color="rgba(0,230,167,0.7)", line=dict(width=1, color="#00e6a7")),
                ))
            fig_dist.add_vline(x=0, line_dash="dash", line_color="#e2e8f0", line_width=1.5)
            # Average win/loss markers
            if wins > 0:
                fig_dist.add_vline(
                    x=df_trades.loc[df_trades["is_win"], "pnl_pct"].mean(),
                    line_dash="dot", line_color=_COLORS["green"], line_width=1,
                    annotation_text="Avg Win", annotation_font_color=_COLORS["green"],
                )
            if losses > 0:
                fig_dist.add_vline(
                    x=df_trades.loc[~df_trades["is_win"], "pnl_pct"].mean(),
                    line_dash="dot", line_color=_COLORS["red"], line_width=1,
                    annotation_text="Avg Loss", annotation_font_color=_COLORS["red"],
                )
            fig_dist.update_layout(
                _layout(
                    height=300, barmode="overlay",
                    title=dict(text="P/L Distribution (%)", font=dict(size=13, color="#c4b5fd")),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, bgcolor="rgba(0,0,0,0)"),
                    xaxis=dict(title="P/L %", gridcolor="#151b27", zeroline=False),
                    yaxis=dict(title="Trades", gridcolor="#151b27", zeroline=False),
                )
            )
            st.plotly_chart(fig_dist, use_container_width=True)

        # ══════════════════════════════════════════════════════════════════════
        # ROW 4 — Hold Time vs Return + Top Winners/Losers
        # ══════════════════════════════════════════════════════════════════════
        scatter_col, ranking_col = st.columns(2)

        with scatter_col:
            # Hold time vs return scatter
            if "hold_hours" in df_trades.columns and df_trades["hold_hours"].sum() > 0:
                fig_scatter = go.Figure()
                fig_scatter.add_trace(go.Scatter(
                    x=df_trades["hold_hours"],
                    y=df_trades["pnl_pct"],
                    mode="markers+text",
                    text=df_trades["symbol"],
                    textposition="top center",
                    textfont=dict(size=9, color="#7cb4ff"),
                    marker=dict(
                        size=12,
                        color=[_COLORS["green"] if w else _COLORS["red"] for w in df_trades["is_win"]],
                        line=dict(width=2, color="#0c0f16"),
                        opacity=0.85,
                    ),
                    hovertemplate="<b>%{text}</b><br>Hold: %{x:.1f}h<br>P/L: %{y:.1f}%<extra></extra>",
                ))
                fig_scatter.add_hline(y=0, line_dash="dash", line_color="#5eead4", line_width=1)
                fig_scatter.update_layout(
                    _layout(
                        height=340, showlegend=False,
                        title=dict(text="Hold Time vs Return", font=dict(size=13, color="#c4b5fd")),
                        xaxis=dict(title="Hours Held", gridcolor="#151b27", zeroline=False),
                        yaxis=dict(title="P/L %", gridcolor="#151b27", zeroline=False),
                    )
                )
                st.plotly_chart(fig_scatter, use_container_width=True)

        with ranking_col:
            # Top 5 Winners and Losers ranking
            st.markdown(
                '<span style="color:#7cb4ff;font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                'letter-spacing:0.5px">🏆 Top Trades Ranking</span>',
                unsafe_allow_html=True,
            )
            top_wins = df_trades.nlargest(5, "pnl_pct")
            top_losses = df_trades.nsmallest(5, "pnl_pct")

            st.markdown(
                '<div style="margin:8px 0 4px;color:#00e6a7;font-size:0.7rem;font-weight:700;letter-spacing:0.5px">'
                '▲ BEST TRADES</div>',
                unsafe_allow_html=True,
            )
            for i, (_, t) in enumerate(top_wins.iterrows(), 1):
                medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i - 1]
                st.markdown(
                    f'<div style="display:flex;justify-content:space-between;align-items:center;'
                    f'padding:8px 12px;background:rgba(0,230,167,0.04);border:1px solid rgba(0,230,167,0.1);'
                    f'border-radius:8px;margin-bottom:4px">'
                    f'<span>{medal} <b style="color:#f1f5f9">{t["symbol"]}</b></span>'
                    f'<span style="color:#00e6a7;font-weight:800;font-family:JetBrains Mono,monospace">'
                    f'+{t["pnl_pct"]:.1f}%</span>'
                    f'<span style="color:#00e6a7;font-size:0.8rem">{fmt_usd(t["pnl_usd"])}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            st.markdown(
                '<div style="margin:12px 0 4px;color:#ff5c5c;font-size:0.7rem;font-weight:700;letter-spacing:0.5px">'
                '▼ WORST TRADES</div>',
                unsafe_allow_html=True,
            )
            for i, (_, t) in enumerate(top_losses.iterrows(), 1):
                skull = ["💀", "☠️", "👎", "📉", "⬇️"][i - 1]
                st.markdown(
                    f'<div style="display:flex;justify-content:space-between;align-items:center;'
                    f'padding:8px 12px;background:rgba(255,92,92,0.04);border:1px solid rgba(255,92,92,0.1);'
                    f'border-radius:8px;margin-bottom:4px">'
                    f'<span>{skull} <b style="color:#f1f5f9">{t["symbol"]}</b></span>'
                    f'<span style="color:#ff5c5c;font-weight:800;font-family:JetBrains Mono,monospace">'
                    f'{t["pnl_pct"]:.1f}%</span>'
                    f'<span style="color:#ff5c5c;font-size:0.8rem">{fmt_usd(t["pnl_usd"])}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        # ══════════════════════════════════════════════════════════════════════
        # ROW 5 — Funnel + Score Distribution + Rejection Reasons
        # ══════════════════════════════════════════════════════════════════════
        funnel_col, score_col = st.columns(2)

        with funnel_col:
            df_funnel = db_query("""
                SELECT funnel_stage, COUNT(*) as count FROM trades
                GROUP BY funnel_stage ORDER BY count DESC
            """)
            if not df_funnel.empty:
                stage_colors = {
                    "DATA_CHECK": _COLORS["blue"], "SAFETY_CHECK": _COLORS["amber"],
                    "PRE_FILTER": "#f97316", "SCORING": _COLORS["purple"],
                    "BUY_EXEC": _COLORS["green"],
                }
                colors = [stage_colors.get(s, _COLORS["muted"]) for s in df_funnel["funnel_stage"]]
                fig = go.Figure(go.Funnel(
                    y=df_funnel["funnel_stage"], x=df_funnel["count"],
                    textinfo="value+percent initial",
                    marker=dict(color=colors),
                    connector=dict(line=dict(color="#151b27")),
                ))
                fig.update_layout(
                    _layout(
                        height=320,
                        title=dict(text="Token Funnel", font=dict(size=13, color="#c4b5fd")),
                    )
                )
                st.plotly_chart(fig, use_container_width=True)

        with score_col:
            df_scores = db_query("""
                SELECT score, decision FROM trades
                WHERE score > 0 AND decision IN ('BUY','REJECT','SKIP','HOLD')
            """)
            if not df_scores.empty:
                fig = px.histogram(
                    df_scores, x="score", color="decision", nbins=20,
                    color_discrete_map={
                        "BUY": _COLORS["green"], "REJECT": _COLORS["red"],
                        "SKIP": _COLORS["muted"], "HOLD": _COLORS["amber"],
                    },
                )
                fig.update_layout(
                    _layout(
                        height=320,
                        title=dict(text="Score Distribution", font=dict(size=13, color="#c4b5fd")),
                        legend=dict(orientation="h", yanchor="bottom", y=1.02, bgcolor="rgba(0,0,0,0)"),
                        xaxis=dict(title="Fusion Score", gridcolor="#151b27", zeroline=False),
                        yaxis=dict(title="Count", gridcolor="#151b27", zeroline=False),
                    )
                )
                fig.add_vline(x=65, line_dash="dash", line_color=_COLORS["green"], line_width=1,
                             annotation_text="Buy threshold", annotation_font_color=_COLORS["green"])
                st.plotly_chart(fig, use_container_width=True)

        # Rejection reasons
        df_rej = db_query("""
            SELECT rejection_reason, COUNT(*) as count FROM trades
            WHERE rejection_reason IS NOT NULL AND rejection_reason != ''
              AND decision NOT IN ('BUY','SELL')
            GROUP BY rejection_reason ORDER BY count DESC LIMIT 12
        """)
        if not df_rej.empty:
            fig = px.bar(
                df_rej, x="count", y="rejection_reason", orientation="h",
                color_discrete_sequence=[_COLORS["blue"]],
            )
            fig.update_layout(
                _layout(
                    height=max(200, len(df_rej) * 30), showlegend=False,
                    title=dict(text="Top Rejection Reasons", font=dict(size=13, color="#c4b5fd")),
                    yaxis=dict(title="", categoryorder="total ascending", gridcolor="#151b27", zeroline=False),
                    xaxis=dict(title="Count", gridcolor="#151b27", zeroline=False),
                )
            )
            st.plotly_chart(fig, use_container_width=True)

    else:
        st.info("No completed trades yet — analytics will appear once you have buy + sell data.")

    st.markdown("---")

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 3 — Coin Explorer (click a coin to see details)
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown('<div class="section-header">🔍 Coin Explorer</div>', unsafe_allow_html=True)

    # Get all unique traded coins
    df_coins = db_query("""
        SELECT
            token_address, symbol,
            MAX(CASE WHEN decision='BUY' THEN entry_price END) as buy_price,
            MAX(CASE WHEN decision='BUY' THEN buy_amount_usd END) as buy_amount,
            MAX(CASE WHEN decision='BUY' THEN timestamp END) as buy_time,
            MAX(CASE WHEN decision='SELL' THEN entry_price END) as sell_price,
            MAX(CASE WHEN decision='SELL' THEN sell_amount_usd END) as sell_amount,
            MAX(CASE WHEN decision='SELL' THEN timestamp END) as sell_time,
            MAX(CASE WHEN decision='BUY' THEN score END) as score,
            COUNT(*) as events
        FROM trades
        WHERE decision IN ('BUY', 'SELL')
        GROUP BY token_address
        ORDER BY MAX(timestamp) DESC
        LIMIT 50
    """)

    if not df_coins.empty:
        from dashboard.db import get_reconciled_positions
        positions = get_reconciled_positions(WALLET_ADDRESS)

        # Build selection list
        coin_options = []
        for _, c in df_coins.iterrows():
            addr = str(c["token_address"])
            sym  = str(c["symbol"] or addr[:8])
            is_open = addr in positions
            tag = "🟢 OPEN" if is_open else "⬛ CLOSED"
            coin_options.append(f"{tag} {sym} ({addr[:12]}...)")

        selected_idx = st.selectbox(
            "Select a coin to explore",
            range(len(coin_options)),
            format_func=lambda i: coin_options[i],
            key="coin_explorer",
        )

        if selected_idx is not None:
            coin = df_coins.iloc[selected_idx]
            addr = str(coin["token_address"])
            sym  = str(coin["symbol"] or addr[:8])
            is_open = addr in positions

            # ── Coin detail panel ─────────────────────────────────────────────
            bp = float(coin["buy_price"] or 0)
            ba = float(coin["buy_amount"] or 0)
            sp = float(coin["sell_price"] or 0)
            sa = float(coin["sell_amount"] or 0)
            bt = str(coin["buy_time"] or "")[:16]
            st_time = str(coin["sell_time"] or "")[:16]
            sc = float(coin["score"] or 0)

            if is_open:
                cp = get_live_price(addr)
                pnl_pct = ((cp - bp) / bp * 100) if bp > 0 and cp > 0 else 0
                pnl_usd = (ba / bp * cp - ba) if bp > 0 and cp > 0 and ba > 0 else 0
                clr = "profit" if pnl_pct >= 0 else "loss"
                sg = "+" if pnl_pct >= 0 else ""
                st.markdown(
                    f'<div style="background:#0c0f16;border:1px solid #151b27;border-radius:12px;padding:20px;margin:10px 0">'
                    f'<div style="display:flex;align-items:center;gap:16px;margin-bottom:16px">'
                    f'<span style="font-size:1.4rem;font-weight:800;color:#f1f5f9">{sym}</span>'
                    f'<span class="badge-profit" style="font-size:0.78rem">OPEN POSITION</span>'
                    f'<span class="{clr}" style="font-size:1.3rem;font-weight:800">{sg}{pnl_pct:.1f}%</span>'
                    f'<span class="{clr}" style="font-size:1rem">{sg}{fmt_usd(abs(pnl_usd))}</span>'
                    f'</div>'
                    f'<div class="detail-grid">'
                    f'<div class="detail-item"><div class="detail-label">Buy Price</div><div class="detail-value">${bp:.8f}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">Current Price</div><div class="detail-value">${cp:.8f}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">Invested</div><div class="detail-value">{fmt_usd(ba)}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">Current Value</div><div class="detail-value">{fmt_usd(ba / bp * cp) if bp > 0 and cp > 0 else "—"}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">Buy Time</div><div class="detail-value">{bt}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">Score</div><div class="detail-value">{sc:.0f}</div></div>'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )
            else:
                pnl_pct = ((sp - bp) / bp * 100) if bp > 0 and sp > 0 else 0
                pnl_usd = sa - ba if sa > 0 and ba > 0 else 0
                clr = "profit" if pnl_pct >= 0 else "loss"
                sg = "+" if pnl_pct >= 0 else ""
                st.markdown(
                    f'<div style="background:#0c0f16;border:1px solid #151b27;border-radius:12px;padding:20px;margin:10px 0">'
                    f'<div style="display:flex;align-items:center;gap:16px;margin-bottom:16px">'
                    f'<span style="font-size:1.4rem;font-weight:800;color:#f1f5f9">{sym}</span>'
                    f'<span style="background:rgba(100,116,139,0.15);color:#7cb4ff;padding:3px 10px;border-radius:6px;font-weight:700;font-size:0.78rem">CLOSED</span>'
                    f'<span class="{clr}" style="font-size:1.3rem;font-weight:800">{sg}{pnl_pct:.1f}%</span>'
                    f'<span class="{clr}" style="font-size:1rem">{sg}{fmt_usd(abs(pnl_usd))}</span>'
                    f'</div>'
                    f'<div class="detail-grid">'
                    f'<div class="detail-item"><div class="detail-label">Buy Price</div><div class="detail-value">${bp:.8f}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">Sell Price</div><div class="detail-value">${sp:.8f}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">Invested</div><div class="detail-value">{fmt_usd(ba)}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">Returned</div><div class="detail-value">{fmt_usd(sa)}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">Buy Time</div><div class="detail-value">{bt}</div></div>'
                    f'<div class="detail-item"><div class="detail-label">Sell Time</div><div class="detail-value">{st_time}</div></div>'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )

            # Live market data for selected coin
            info = get_token_full_info(addr)
            if info:
                st.markdown(
                    '<span style="color:#7cb4ff;font-size:0.75rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">'
                    '📡 Live Market Data</span>',
                    unsafe_allow_html=True,
                )
                mc1, mc2, mc3, mc4 = st.columns(4)
                mc1.markdown(kpi_card("Price", f"${info.get('price', 0):.8f}", fmt_pct(info.get('change_1h', 0)) + " 1h"), unsafe_allow_html=True)
                mc2.markdown(kpi_card("Liquidity", fmt_usd(info.get('liquidity', 0)), ""), unsafe_allow_html=True)
                mc3.markdown(kpi_card("Market Cap", fmt_usd(info.get('market_cap', 0)), ""), unsafe_allow_html=True)
                b1h = info.get("buys_1h", 0)
                s1h = info.get("sells_1h", 0)
                ratio = (b1h / (b1h + s1h) * 100) if (b1h + s1h) > 0 else 50
                ratio_clr = "profit" if ratio > 50 else "loss"
                mc4.markdown(kpi_card("Buy/Sell 1h",
                    f'{b1h}/{s1h}',
                    f'<span class="{ratio_clr}">{ratio:.0f}% buys</span>'
                ), unsafe_allow_html=True)

            # Links
            st.markdown(
                f'<div style="display:flex;gap:16px;margin-top:10px">'
                f'<a href="https://dexscreener.com/solana/{addr}" target="_blank" style="color:#3b8bff;font-size:0.82rem;text-decoration:none">📊 DexScreener</a>'
                f'<a href="https://solscan.io/token/{addr}" target="_blank" style="color:#3b8bff;font-size:0.82rem;text-decoration:none">🔗 Solscan</a>'
                f'<a href="https://rugcheck.xyz/tokens/{addr}" target="_blank" style="color:#3b8bff;font-size:0.82rem;text-decoration:none">🛡️ RugCheck</a>'
                f'<a href="https://jup.ag/swap/SOL-{addr}" target="_blank" style="color:#3b8bff;font-size:0.82rem;text-decoration:none">🪐 Jupiter</a>'
                f'</div>',
                unsafe_allow_html=True,
            )
    else:
        st.info("No traded coins found yet.")
